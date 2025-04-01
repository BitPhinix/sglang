import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import torch
from sglang.srt.distributed import get_tensor_model_parallel_rank

if TYPE_CHECKING:
    from sglang.srt.model_executor.forward_batch_info import ForwardBatch, ForwardMode


def compute_split_seq_index(
    forward_mode: "ForwardMode",
    num_tokens: int,
    extend_lens: Optional[Sequence[int]],
) -> Optional[int]:
    if forward_mode.is_extend():
        assert extend_lens is not None
        split_seq_index = _split_array_by_half_sum(extend_lens)
        num_seqs = len(extend_lens)
    elif forward_mode.is_decode():
        split_seq_index = num_tokens // 2
        num_seqs = num_tokens
    else:
        raise NotImplementedError

    if split_seq_index == 0 or split_seq_index == num_seqs:
        return None

    return split_seq_index


def _split_array_by_half_sum(arr: Sequence[int]) -> int:
    overall_sum = sum(arr)
    accumulator, split_index = 0, 0
    for value in arr[:-1]:
        accumulator += value
        split_index += 1
        if accumulator >= overall_sum // 2:
            break
    return split_index


def compute_split_token_index(
    split_seq_index: int,
    forward_mode: "ForwardMode",
    extend_seq_lens: Optional[Sequence[int]],
) -> int:
    if forward_mode.is_extend():
        assert extend_seq_lens is not None
        return sum(extend_seq_lens[:split_seq_index])
    elif forward_mode.is_decode():
        return split_seq_index
    else:
        raise NotImplementedError


def model_forward_split_inputs(
    hidden_states: torch.Tensor,
    residual: torch.Tensor,
    positions: torch.Tensor,
    forward_batch: "ForwardBatch",
) -> Tuple[Dict, Dict]:
    return tuple(
        [
            _model_forward_filter_inputs(
                hidden_states=hidden_states,
                residual=residual,
                positions=positions,
                output_forward_batch=output_forward_batch,
                tbo_subbatch_index=tbo_subbatch_index,
            )
            for tbo_subbatch_index, output_forward_batch in enumerate(
            forward_batch.tbo_children
        )
        ]
    )


def _model_forward_filter_inputs(
    hidden_states: torch.Tensor,
    residual: torch.Tensor,
    positions: torch.Tensor,
    output_forward_batch: "ForwardBatch",
    tbo_subbatch_index: int,
) -> Dict:
    token_slice = slice(*output_forward_batch.tbo_parent_token_range)
    return dict(
        hidden_states=hidden_states[token_slice],
        residual=residual[token_slice],
        positions=positions[token_slice],
        forward_batch=output_forward_batch,
        tbo_subbatch_index=tbo_subbatch_index,
    )


def model_forward_merge_outputs(output_a, output_b):
    def _handle_key(name):
        return torch.concat([output_a[name], output_b[name]], dim=0)

    return _handle_key("hidden_states"), _handle_key("residual")


_ENABLE_PROFILE = bool(int(os.environ.get("SGLANG_TBO_ENABLE_PROFILE", "0")))

if _ENABLE_PROFILE:
    import nvtx


class YieldOperation:
    pass


@dataclass
class AnnotationOperation:
    debug_name: str


@dataclass
class ExecutableOperation:
    debug_name: Optional[str]
    fn: Callable


Operation = Union[YieldOperation, AnnotationOperation, Callable]
Stage = List[Callable]


def model_forward_execute_two_batch(
    inputs,
    operations: List[Operation],
    delta_stages: int,
):
    splitted_inputs = model_forward_split_inputs(**inputs)
    inputs_a, inputs_b = splitted_inputs
    output_a, output_b = _execute_two_batch_raw(
        inputs_a, inputs_b, operations, delta_stages=delta_stages
    )
    return model_forward_merge_outputs(output_a, output_b)


def _execute_two_batch_raw(inputs_a, inputs_b, operations, delta_stages: int):
    stages = _convert_operations_to_stages(operations)
    executor_a = _StageExecutor("a", stages, inputs=inputs_a)
    executor_b = _StageExecutor("b", stages, inputs=inputs_b)

    for _ in range(delta_stages):
        executor_a.next()

    for _ in range(executor_a.num_stages - delta_stages):
        executor_a.next()
        executor_b.next()

    for _ in range(delta_stages):
        executor_b.next()

    assert executor_a.done and executor_b.done
    return executor_a.output, executor_b.output


class _StageExecutor:
    def __init__(self, debug_name: str, stages: List[Stage], inputs):
        self._debug_name = debug_name
        self._stages = stages
        self._index = 0
        self._stage_state = _StateDict()
        self._stage_output = inputs

    def next(self):
        assert not self.done

        stage = self._stages[self._index]

        with _annotate_region(debug_name=f"{self._debug_name}{self._index}"):
            for op in stage:
                with _annotate_region(
                    debug_name=op.__name__.replace("_forward_tbo_op_", "")
                ):
                    self._stage_output = op(
                        state=self._stage_state, **(self._stage_output or {})
                    )

        self._index += 1

    @property
    def output(self):
        assert self.done
        return self._stage_output

    @property
    def done(self):
        return self._index >= self.num_stages

    @property
    def num_stages(self):
        return len(self._stages)


@contextmanager
def _annotate_region(debug_name):
    if _ENABLE_PROFILE:
        with torch.autograd.profiler.record_function(debug_name):
            with nvtx.annotate(debug_name):
                yield
    else:
        yield


class _StateDict:
    def __init__(self):
        self._data = {}

    def __setattr__(self, key, value):
        if key == "_data":
            super().__setattr__(key, value)
            return
        assert (
            key not in self._data
        ), f"`{key}` already exist, are you sure you want to override it?"
        self._data[key] = value

    def __getattr__(self, item):
        return self._data[item]

    def update(self, values: Dict[str, Any]):
        for k, v in values.items():
            setattr(self, k, v)

    def clear(self):
        self._data.clear()


def _convert_operations_to_stages(operations: List[Operation]) -> List[Stage]:
    operation_chunks = list(
        _chunk_by_separator(operations, lambda op: isinstance(op, YieldOperation))
    )
    assert all(len(chunk) > 0 for chunk in operation_chunks)
    return operation_chunks


def _chunk_by_separator(
    items: List[Any], is_separator: Callable[[Any], bool]
) -> Generator[List[Any], None, None]:
    pending_items = []
    for item in items:
        if is_separator(item):
            yield pending_items
            pending_items = []
        else:
            pending_items.append(item)
    if len(pending_items) > 0:
        yield pending_items

import contextlib
import functools
import threading
import typing
from math import prod
from typing import Callable, Mapping, Optional, Sequence, TypeVar, Union

import equinox as eqx
import jax

# TODO: avoid depending on private Equinox internals.
from equinox._compile_utils import compile_cache

# from jax._src.sharding_impls import AUTO
from jax.experimental.pjit import pjit
from jax.lax import with_sharding_constraint
from jax.sharding import Mesh, NamedSharding, PartitionSpec, SingleDeviceSharding
from jaxtyping import PyTree

from .axis import Axis, AxisSelection, AxisSelector
from .core import NamedArray
from .jax_utils import Static, is_jax_array_like
from .tree_util import hashable_combine, hashable_partition
from .util import StringHolderEnum, ensure_tuple, is_named_array


PhysicalAxisSpec = Union[(str), Sequence[str]]
ResourceMapping = Mapping[(str), PhysicalAxisSpec]
"""Mapping from logical axis names to physical axis names"""

F = typing.TypeVar("F", bound=typing.Callable)


class ResourceAxis(StringHolderEnum):
    """Standard names for physical axes"""

    MODEL = "model"
    DATA = "data"


class _ResourceMappingHolder:
    """Global resource mapping, used with a context manager to give dynamic scoping to resource mappings"""

    def __init__(self):
        self.thread_data = threading.local()
        self.thread_data.resource_mapping = None


_mapping_holder = _ResourceMappingHolder()


@contextlib.contextmanager
def axis_mapping(mapping: ResourceMapping, *, merge: bool = False, **kwargs):
    """Context manager for setting the global resource mapping"""
    mapping = dict(mapping)

    old_mapping = current_thread_local_mapping()
    if merge:
        mapping.update(old_mapping or {})

    if len(kwargs):
        mapping.update(kwargs)

    _mapping_holder.thread_data.resource_mapping = mapping
    try:
        yield
    finally:
        _mapping_holder.thread_data.resource_mapping = old_mapping


def current_thread_local_mapping():
    """
    Get the current thread-local resource mapping, or None if there is no resource mapping set.
    :return:
    """
    if _mapping_holder.thread_data is None:
        return None
    if not hasattr(_mapping_holder.thread_data, "resource_mapping"):
        return None

    return _mapping_holder.thread_data.resource_mapping


T = TypeVar("T", bound=PyTree)


def auto_sharded(x: T, mesh: Optional[Mesh] = None) -> T:
    """
    Shard a PyTree using the global axis mapping. NamedArrays in the PyTree are sharded using the axis mapping
     and the names in the tree.

    If there is no axis mapping, the global axis mapping, this function is a no-op.
    """
    mapping = current_thread_local_mapping()

    if mapping is None:
        return x

    return shard_with_axis_mapping(x, mapping, mesh)


def shard_with_axis_mapping(x: T, mapping: ResourceMapping, mesh: Optional[Mesh] = None) -> T:
    """
    Shard a PyTree using the provided axis mapping. NamedArrays in the PyTree are sharded using the axis mapping.
    Other arrays are not sharded (unless they're already sharded).

    Inside of a jit context, this method grounds out in calls to `with_sharding_constraint`. Outside of a jit
    context, this method grounds out in either device_put or make_array_from_callback, depending on whether the
    resulting sharding spans more than one host.
    """

    def _do_device_put(x):
        if not is_named_array(x):
            return x

        if _is_jit_tracer(x.array):
            pspec = pspec_for_axis(x.axes, mapping)
            return with_sharding_constraint(x, pspec)
        elif not is_jax_array_like(x.array):
            # this happens when we filter out params for things like lora
            return x
        else:
            raw_x = x.array
            current_sharding = raw_x.sharding

            desired_sharding = infer_resource_partitions(
                x, mapping, mesh=mesh, preserve_existing_shardings=False
            ).array

            if current_sharding.is_equivalent_to(desired_sharding, ndim=raw_x.ndim):
                return x
            elif desired_sharding.is_fully_addressable:
                raw_x = jax.device_put(raw_x, desired_sharding)
                return NamedArray(raw_x, x.axes)
            else:
                # if the sharding is not fully addressable, we can't use device_put, so we use this hacky workaround.
                # TODO: we lose "src" information, but i think that's only for autodiff, and this isn't an autodiff
                # context, I think?
                shape = raw_x.shape
                raw_x = jax.make_array_from_callback(shape, desired_sharding, lambda index: raw_x[index])
                return NamedArray(raw_x, x.axes)

    return jax.tree_util.tree_map(_do_device_put, x, is_leaf=is_named_array)


def infer_resource_partitions(
    tree: PyTree,
    resource_mapping: Optional[ResourceMapping] = None,
    preserve_existing_shardings: bool = True,
    use_auto_sharding: bool = True,
    mesh: Optional[Mesh] = None,
) -> PyTree:
    """
    Infer the sharding for a module, to be used with named_jit.
    The basic idea is to tree all NamedArrays as leaves for the purposes of this function,
    and to create NamedShardings from those names plus the resource_mapping.
    If preserve_existing_shardings is True, then NamedArrays that are already sharded are left alone.

    If resource_mapping is not provided, this function attempts to use the global resource mapping.

    If use_auto_sharding is True, then we use the new experimental AUTO-sharding feature, which is not yet
    fully supported by JAX. If it is False, then we will guess fully replicated for any unnamed arrays that
    don't have a sharding.
    """
    if resource_mapping is None:
        resource_mapping = current_thread_local_mapping()

    if resource_mapping is None:
        raise ValueError("No resource mapping found")

    mesh = mesh or _get_mesh()

    def partition_spec(node: typing.Any):
        if isinstance(node, NamedArray):
            if preserve_existing_shardings:
                current_sharding = getattr(node, "sharding", None)
            else:
                current_sharding = None

            if current_sharding is not None:
                return NamedArray(current_sharding, node.axes)  # type: ignore
            else:
                sharding = NamedSharding(mesh, pspec_for_axis(node.axes, resource_mapping))
                return NamedArray(sharding, node.axes)  # type: ignore
        elif is_jax_array_like(node):
            sharding = getattr(node, "sharding", None)
            # TODO: these are usually replicated. Is there a better way to tell?
            if isinstance(sharding, SingleDeviceSharding):
                return NamedSharding(mesh, PartitionSpec(None))
            elif sharding is not None:
                return sharding
            elif node.shape == ():
                return NamedSharding(mesh, PartitionSpec())
            # elif use_auto_sharding:
            # TODO: auto doesn't seem to really work reliably yet
            #     compat between 0.4.10 and 0.4.11
            # if isinstance(AUTO, typing.Callable):  # type: ignore
            #     return AUTO(mesh)
            # else:
            #     return AUTO
            return NamedSharding(mesh, PartitionSpec(None))
        else:
            return None

    return jax.tree_util.tree_map(partition_spec, tree, is_leaf=is_named_array)


def named_jit(
    fn: Callable = None,
    axis_resources: Optional[ResourceMapping] = None,
    *,
    in_axis_resources: Optional[ResourceMapping] = None,
    out_axis_resources: Optional[ResourceMapping] = None,
    donate_args: Optional[PyTree] = None,
    donate_kwargs: Optional[PyTree] = None,
    **pjit_args,
):
    """
    A version of pjit that uses NamedArrays and the provided resource mapping to infer resource partitions for
    sharded computation for.

    `axis_resources` will be used for a context-specific resource mapping when the function is invoked.
    In addition, if in_axis_resources is not provided, the arguments' own (pre-existing) shardings will be used as the in_axis_resources.
    If out_axis_resources is not provided, axis_resources will be used as the out_axis_resources.

    If no resource mapping is provided, this function attempts to use the context resource mapping.

    Functionally this is very similar to something like:

    ```python
     arg = hax.shard_with_axis_mapping(arg, in_axis_resources)
     with hax.axis_mapping(axis_resources):
        result = jax.jit(fn, **pjit_args)(arg)
    result = hax.shard_with_axis_mapping(result, out_axis_resources)
    return result
    ```

    Args:
        fn (Callable, optional): The function to be jit'd.
        axis_resources (ResourceMapping, optional): A mapping from logical axis names to physical axis names use for th
                e context-specific resource mapping.
        in_axis_resources (ResourceMapping, optional): A mapping from logical axis names to physical axis names for
                arguments. If not passed, it uses the argument's own shardings.
        out_axis_resources (ResourceMapping, optional): A mapping from logical axis names to physical axis names for the
                result, defaults to axis_resources.
        donate_args (PyTree, optional): A PyTree of booleans or function leaf->bool, indicating if the arguments should
                be donated to the computation.
        donate_kwargs (PyTree, optional): A PyTree of booleans or function leaf->bool, indication if the keyword
                arguments should be donated to the computation.

    Returns:
        A jit'd version of the function.
    """

    if fn is None:
        return functools.partial(
            named_jit,
            axis_resources=axis_resources,
            in_axis_resources=in_axis_resources,
            out_axis_resources=out_axis_resources,
            donate_args=donate_args,
            donate_kwargs=donate_kwargs,
            **pjit_args,
        )

    @functools.wraps(fn)
    def f(*args, **kwargs):
        nonlocal axis_resources, in_axis_resources, out_axis_resources, donate_args, donate_kwargs

        if axis_resources is None:
            axis_resources = current_thread_local_mapping()

        if out_axis_resources is None:
            out_axis_resources = axis_resources

        dynamic_fun, static_fun = hashable_partition(fn, is_jax_array_like)
        dynamic_argspec, static_argspec = hashable_partition((args, kwargs), is_jax_array_like)
        dynamic = (dynamic_fun, dynamic_argspec)

        if donate_args is not None or donate_kwargs is not None:
            if donate_args is None:
                dargs = (False,) * len(args)
            elif isinstance(donate_args, bool):
                dargs = (donate_args,) * len(args)
            elif not isinstance(donate_args, tuple):
                dargs = tuple(donate_args)
            else:
                dargs = donate_args

            if len(dargs) < len(args):
                dargs = dargs + (False,) * (len(args) - len(dargs))

            if len(dargs) != len(args):
                raise ValueError(f"Expected {len(args)} donate_args, got {len(dargs)}")

            dkwargs = donate_kwargs or {k: False for k in kwargs}
            dkwargs = {k: dkwargs.get(k, False) for k in kwargs}
            dynamic_donated, dynamic_reserved = eqx.partition(dynamic, (False, (dargs, dkwargs)))
        else:
            dynamic_donated = jax.tree_util.tree_map(lambda _: None, dynamic)
            dynamic_reserved = dynamic

        static = (static_fun, static_argspec)

        if axis_resources is not None:
            cmanager = axis_mapping(axis_resources)
        else:
            cmanager = contextlib.nullcontext()

        with cmanager:
            output_shape = _cached_filter_eval_shape(fn, *args, **kwargs)
            # TODO: with new jax.Array I shouldn't have to specify shardings, but I do for now
            #  https://github.com/google/jax/issues/15600
            # we don't really need in_shardings though
            my_pjit_args = dict(**pjit_args)

            if in_axis_resources is not None or axis_resources is not None:
                if in_axis_resources is None:
                    in_axis_resources = axis_resources
                in_resources = infer_resource_partitions(
                    (dynamic_donated, dynamic_reserved),
                    in_axis_resources,
                    preserve_existing_shardings=in_axis_resources is None,
                )
                my_pjit_args["in_shardings"] = in_resources

            if out_axis_resources is not None:
                # TODO: when AUTO is fixed (or eval_shape can give shardings), use it here
                out_resources = infer_resource_partitions(output_shape, out_axis_resources, use_auto_sharding=False)
                my_pjit_args["out_shardings"] = out_resources

            cached_pjitted_fun = _named_pjit_cache(fn, **my_pjit_args)
            out, out_static = cached_pjitted_fun(dynamic_donated, dynamic_reserved, static)
            out = hashable_combine(out, out_static.value)

            return out

    return f


@typing.overload
def fsdp(fn: F, parameter_mapping: ResourceMapping, compute_mapping: ResourceMapping) -> F:
    ...


@typing.overload
def fsdp(parameter_mapping: ResourceMapping, compute_mapping: ResourceMapping) -> typing.Callable[[F], F]:
    ...


def fsdp(*args, **kwargs):
    """
    A convenience wrapper around named_jit / pjit to encode the FSDP pattern. It's basically equivalent to this:

    ```python
    @named_jit(in_axis_resources=parameter_mapping, out_axis_resources=parameter_mapping, axis_resources=compute_mapping)
    def f(*args, **kwargs):
        return fn(*args, **kwargs)
    ```

    This function can be used as a decorator or as a function.
    """
    if "fn" in kwargs:
        return _fsdp_impl(*args, **kwargs)
    elif len(args) > 1 and callable(args[0]):
        return _fsdp_impl(*args, **kwargs)
    else:
        return lambda fn: _fsdp_impl(fn, *args, **kwargs)


def _fsdp_impl(fn: F, parameter_mapping, compute_mapping):
    return named_jit(
        fn, in_axis_resources=parameter_mapping, out_axis_resources=parameter_mapping, axis_resources=compute_mapping
    )


# This is more or less copy-pasted from Equinox's similar functions (pmap, vmap, etc), but
# it's not really explained there so we'll explain it here.
# Many jax functions work by compiling functions to XLA. The compilation process is expensive,
# so we want to cache the compiled functions. However, the compiled functions are tied to the
# "static" arguments to the functions. This is particularly important for a library like Equinox,
# which Haliax is built on top of, because Equinox uses pytrees extensively for modules, and mixes "static"
# configuration with "dynamic" data.
# Thus we need to carefully partition the arguments to the function into "static" and "dynamic" arguments,
# and cache our compiled functions based on the static arguments.
# In Equinox conceptually there are three types of "arguments": positional, named, and the function itself.
# All of these are pytrees, and we need to partition them into static and dynamic arguments.
# Inside the function, we then combine the arguments into a single pytree, and pass that to the original function.
# With pjit we also have "donated" arguments, which are arguments that we promise not to use after the function
# returns. This is useful for conserving memory, but we also have to splice them back in.
# Also recall that a "pytree" can split into leaves and a "treedef", which can then be reconstructed.
@compile_cache
def _named_pjit_cache(fun_names, **jitkwargs):
    def fun_wrapped(dynamic_donated, dynamic_reserved, static):
        dynamic = eqx.combine(dynamic_donated, dynamic_reserved)
        dynamic_fun, dynamic_spec = dynamic
        static_fun, static_spec = static

        fun = hashable_combine(dynamic_fun, static_fun)
        args, kwargs = hashable_combine(dynamic_spec, static_spec)
        out = fun(*args, **kwargs)
        out_dynamic, out_static = hashable_partition(out, is_jax_array_like)
        return out_dynamic, Static(out_static)

    fun_name, fun_qualname = fun_names
    fun_wrapped.__name__ = fun_name
    fun_wrapped.__qualname__ = fun_qualname

    jitkwargs = dict(jitkwargs)
    if "out_shardings" in jitkwargs:
        out_shardings = jitkwargs["out_shardings"]
        # None for the static
        jitkwargs["out_shardings"] = (out_shardings, None)

    # TODO: jit should work here, but there's a weird error. see if it goes away on its own
    return pjit(
        fun_wrapped,
        donate_argnums=0,
        static_argnums=2,
        **jitkwargs,
    )


_eval_shape_cache = {}


def _cached_filter_eval_shape(fun, *args, **kwargs):
    """
    eval_shape is surprisingly expensive, so we cache it. We use this for named_pjit for evaluating resource partitions
    of the output.
    """
    dynamic, static = hashable_partition((fun, args, kwargs), is_jax_array_like)
    if static not in _eval_shape_cache:
        _eval_shape_cache[static] = eqx.filter_eval_shape(fun, *args, **kwargs)

    return _eval_shape_cache[static]


def physical_axis_name(axis: AxisSelector, mapping: Optional[ResourceMapping] = None) -> Optional[PhysicalAxisSpec]:
    """Get the physical axis name for a logical axis from the mapping. Returns none if the axis is not mapped."""
    if mapping is None:
        mapping = current_thread_local_mapping()
    if mapping is None:
        return None
    elif isinstance(axis, str):
        return mapping.get(axis, None)
    else:
        return mapping.get(axis.name, None)


def physical_axis_size(axis: AxisSelector, mapping: Optional[ResourceMapping] = None) -> Optional[int]:
    """Get the physical axis size for a logical axis. This is the product of the size of all physical axes
    that this logical axis is mapped to."""
    # TODO: shouldn't be accessing this internal api, but...
    from jax.experimental.maps import thread_resources

    try:
        mesh_shape = thread_resources.env.shape
    except AttributeError:
        raise ValueError("No resource mapping found")

    name: Union[None, str, Sequence[str]] = physical_axis_name(axis, mapping)
    if name is None:
        return None
    elif isinstance(name, str):
        name = (name,)

    return prod([mesh_shape[n] for n in name])


def sharding_for_axis(
    axis: AxisSelection, mapping: Optional[ResourceMapping] = None, mesh: Optional[Mesh] = None
) -> NamedSharding:
    """Get the sharding for a single axis"""
    return NamedSharding(mesh or _get_mesh(), pspec_for_axis(axis, mapping))


def pspec_for_axis(axis: AxisSelection, mapping: Optional[ResourceMapping] = None) -> PartitionSpec:
    """Get the PartitionSpec for a single axis"""
    axis = ensure_tuple(axis)
    return PartitionSpec(*(physical_axis_name(a, mapping) for a in axis))


def round_axis_for_partitioning(axis: Axis, mapping: Optional[ResourceMapping] = None) -> Axis:
    """Round an axis so that it's divisible by the size of the partition it's on"""
    size = physical_axis_size(axis, mapping)
    if size is None:
        return axis
    else:
        new_size = (axis.size + size - 1) // size * size
        return Axis(axis.name, new_size)


def _get_mesh():
    from jax.experimental.maps import thread_resources

    return thread_resources.env.physical_mesh


def _is_jit_tracer(x) -> bool:
    if isinstance(x, NamedArray):
        x = x.array
    return isinstance(x, jax.core.Tracer)


__all__ = [
    "PhysicalAxisSpec",
    "ResourceAxis",
    "ResourceMapping",
    "axis_mapping",
    "auto_sharded",
    "infer_resource_partitions",
    "named_jit",
    "fsdp",
    "physical_axis_name",
    "pspec_for_axis",
    "round_axis_for_partitioning",
    "current_thread_local_mapping",
]

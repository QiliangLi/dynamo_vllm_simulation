"""Narrow adapter for Ascend BalanceScheduler's disabled-balance path.

No vLLM imports here. The supplied patcher must retain its source/anchor checks.
The base class is never mutated. Enabled balance scheduling is unsupported.
"""
import ast
import functools
import inspect
import textwrap
import types
from dataclasses import dataclass


BALANCE_MODULE = 'patched.ascend_balance_schedule'


def method_owner(cls, name='schedule'):
    for owner in cls.__mro__:
        if name in owner.__dict__:
            value = owner.__dict__[name]
            if not isinstance(value, types.FunctionType):
                raise RuntimeError('schedule must be an ordinary Python method')
            return owner, value
    raise RuntimeError('schedule method not found')


def compile_method(tree, original):
    """Compile a transformed function while retaining original closure cells.

Setting __qualname__ or binding self does not restore a zero-argument super().
The factory supplies lexical bindings; FunctionType reuses the original cells.
"""
    fn = tree.body[0]
    if not isinstance(fn, ast.FunctionDef) or fn.decorator_list:
        raise RuntimeError('unsupported decorated/async scheduler function')
    names = original.__code__.co_freevars
    factory = ast.FunctionDef(
        name='_tfs_compile_factory',
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=n) for n in names],
                           kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[fn, ast.Return(value=ast.Name(id=fn.name, ctx=ast.Load()))],
        decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    scope = {}
    exec(compile(module, '<storage-read-admission-v1>', 'exec'), original.__globals__, scope)
    cells = dict(zip(names, original.__closure__ or ()))
    compiled = scope['_tfs_compile_factory'](*(cells[n].cell_contents for n in names))
    if set(compiled.__code__.co_freevars) != set(names):
        raise RuntimeError('transformation changed scheduler closure layout')
    result = types.FunctionType(compiled.__code__, original.__globals__, original.__name__,
                                original.__defaults__,
                                tuple(cells[n] for n in compiled.__code__.co_freevars) or None)
    result.__kwdefaults__ = original.__kwdefaults__
    functools.update_wrapper(result, original)
    return result


@dataclass(frozen=True)
class SchedulePatch:
    method: object
    target_owner: type
    balance_wrapper: bool

    def check_config(self, config):
        additional = getattr(config, 'additional_config', None) or {}
        if self.balance_wrapper and bool(additional.get('enable_balance_scheduling', False)):
            raise RuntimeError('TFS adapter supports enable_balance_scheduling=false only')

    def check_instance(self, instance):
        if self.balance_wrapper and getattr(instance, '_balance_enabled', None) is not False:
            raise RuntimeError('BalanceScheduler runtime flag must be exactly False')


def prepare_schedule_patch(cls, patcher):
    owner, original = method_owner(cls)
    if owner.__module__ != BALANCE_MODULE or owner.__name__ != 'BalanceScheduler':
        return SchedulePatch(patcher(original), owner, False)

    if hasattr(original, '__wrapped__'):
        raise RuntimeError('BalanceScheduler is already wrapped; verify plugin order')
    tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
    fn = tree.body[0]
    expected = ast.parse('if not self._balance_enabled:\n    return super().schedule()').body[0]
    if (not isinstance(fn, ast.FunctionDef) or fn.decorator_list
            or len(fn.args.args) != 1 or fn.args.args[0].arg != 'self'
            or fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg
            or fn.args.defaults
            or ast.dump(fn.body[0]) != ast.dump(expected)):
        raise RuntimeError('BalanceScheduler disabled-path guard changed; review required')
    closure = dict(zip(original.__code__.co_freevars, original.__closure__ or ()))
    if '__class__' not in closure or closure['__class__'].cell_contents is not owner:
        raise RuntimeError('BalanceScheduler original __class__ closure is unavailable')

    # Follow the real MRO after the defining owner, not a rebinding of Scheduler.
    after = cls.__mro__[cls.__mro__.index(owner) + 1:]
    target_owner = next((base for base in after if 'schedule' in base.__dict__), None)
    if target_owner is None:
        raise RuntimeError('BalanceScheduler parent schedule not found')
    parent = target_owner.__dict__['schedule']
    if not isinstance(parent, types.FunctionType) or hasattr(parent, '__wrapped__'):
        raise RuntimeError('parent schedule is not an unwrapped Python function')
    patched = patcher(parent)  # Keep the exact parent-source hash/AST validation.

    @functools.wraps(original)
    def schedule(self):
        if getattr(self, '_balance_enabled', None) is not False:
            raise RuntimeError('TFS disabled-balance adapter cannot run balance scheduling')
        return patched(self)

    schedule._storage_read_admission = True
    return SchedulePatch(schedule, target_owner, True)

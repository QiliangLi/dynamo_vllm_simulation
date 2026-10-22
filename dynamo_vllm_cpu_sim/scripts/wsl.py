#!/usr/bin/env python3
"""Run the CPU simulator from Windows through an isolated WSL2 workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
EXCLUDES = ('/.venv', '/upstream', '/results', '__pycache__', '*.pyc', '/.wsl-source.json')


def mirror(source, destination):
    """Sync edited code, preserve Linux environments/results, and remove stale code."""
    source, destination = source.resolve(), destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise RuntimeError('源目录与 Linux 工作目录不能互相包含。')
    marker = destination / '.wsl-source.json'
    identity = {'source': str(source)}
    if destination.exists() and any(destination.iterdir()):
        try:
            matches = marker.is_file() and json.loads(marker.read_text()) == identity
        except (OSError, ValueError):
            matches = False
        if not matches:
            raise RuntimeError(f'保留来源不明的目录，停止同步：{destination}')
    destination.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(identity) + '\n')
    subprocess.run(['rsync', '-a', '--delete', *[f'--exclude={p}' for p in EXCLUDES],
                    str(source) + '/', str(destination) + '/'], check=True)
    # Also accept an existing Windows checkout created before .gitattributes was added.
    for script in (destination / 'scripts').glob('*.sh'):
        data = script.read_bytes()
        if b'\r\n' in data:
            script.write_bytes(data.replace(b'\r\n', b'\n'))


def runtime_env(tools):
    env = {**os.environ, 'PATH': str(tools / 'bin') + os.pathsep + os.environ.get('PATH', ''),
           'PYTHONHASHSEED': '0', 'VLLM_PLUGINS': '', 'HF_HUB_OFFLINE': '1'}
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
        env.setdefault(name, '1')
    return env


def state_path(value):
    state = Path(value or '~/.local/share/dynamo-vllm-simulation').expanduser()
    if not state.is_absolute() or state.resolve().is_relative_to('/mnt'):
        raise RuntimeError('--state-root 必须是 Linux 文件系统中的绝对路径，不能放在 /mnt 下。')
    return state.resolve()


def reject_other_stacks():
    # The existing launch_stack.sh stop command is global; do not let it stop other work.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            name = (entry / 'comm').read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if ('EngineCore' in name or b'dynamo.frontend' in args
                or any(Path(os.fsdecode(a)).name == 'stack_vllm_worker.py' for a in args)):
            raise RuntimeError(f'检测到其他仿真 / EngineCore 进程 {entry.name}；请先结束它，再运行 stack。')


def preserve_stack_logs(work, arguments, log_root=Path('/tmp/stack_logs')):
    name = 'stack_remote'
    if '--name' in arguments and arguments.index('--name') + 1 < len(arguments):
        name = arguments[arguments.index('--name') + 1]
    source = (log_root / name).resolve()
    destination = (work / 'results' / name / 'logs').resolve()
    if (not source.is_relative_to(log_root.resolve())
            or not destination.is_relative_to((work / 'results').resolve())):
        raise RuntimeError('日志路径必须位于本次实验目录内。')
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('*.sock', '*.pid'))
        print(f'已保留进程日志：{destination}', flush=True)


def linux_main(args):
    if sys.platform != 'linux' or sys.version_info[:2] != (3, 12):
        raise RuntimeError('WSL 中需要 Linux Python 3.12；推荐 Ubuntu-24.04。')
    if shutil.which('rsync') is None:
        raise RuntimeError('请在 WSL 中安装：sudo apt-get install python3-venv rsync')
    state = state_path(args.state_root)
    key = hashlib.sha256(str(SOURCE).encode()).hexdigest()[:12]
    work = state / key
    mirror(SOURCE, work)
    tools = state / 'tools'
    python = work / '.venv/bin/python'
    env = runtime_env(tools)
    print(f'Linux 工作目录：{work}', flush=True)
    print(f'结果目录：{work / "results"}', flush=True)
    if args.action == 'setup':
        if args.arguments not in ([], ['--stack']):
            raise RuntimeError('setup 仅支持可选参数 --stack。')
        tool_python = tools / 'bin/python'
        if not tool_python.is_file():
            subprocess.run([sys.executable, '-m', 'venv', str(tools)], check=True)
        subprocess.run([str(tool_python), '-m', 'pip', 'install', 'uv==0.13.4'], check=True)
        subprocess.run(['bash', 'scripts/bootstrap.sh'], cwd=work, env=env, check=True)
        if args.arguments:
            subprocess.run(['bash', 'scripts/bootstrap_stack.sh'], cwd=work, env=env, check=True)
        return 0
    if not python.is_file():
        raise RuntimeError('此克隆尚未准备环境，请先执行同一个入口的 setup --stack。')
    # Runtime traffic stays local. Keep proxies available for the separate setup action.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    env['NO_PROXY'] = '*'
    commands = {
        'doctor': [str(python), 'scripts/doctor.py'],
        'test': [str(python), '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
        'run': [str(python), '-m', 'sim.run'],
        'python': [str(python)],
        'stack': ['bash', 'scripts/run_stack_experiment.sh'],
    }
    if args.action == 'stack':
        reject_other_stacks()
    if args.action == 'python' and not args.arguments:
        raise RuntimeError('python 动作需要脚本路径、-m 模块或 -c 代码。')
    if args.action == 'run' and not any(a == '--output' or a.startswith('--output=') for a in args.arguments):
        from datetime import datetime
        args.arguments += ['--output', 'results/wsl-' + datetime.now().strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}']
    result = subprocess.call(commands[args.action] + args.arguments, cwd=work, env=env)
    if args.action == 'stack':
        # WSL can stop after the last process exits; /tmp may be cleared on next boot.
        preserve_stack_logs(work, args.arguments)
    return result


def main():
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distro', default=os.environ.get('DV_WSL_DISTRO', 'Ubuntu-24.04'))
    parser.add_argument('--state-root', help='Linux 绝对路径；默认放在 WSL 用户目录')
    parser.add_argument('--inside-wsl', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('action', choices=['setup', 'doctor', 'test', 'run', 'stack', 'python'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.arguments[:1] == ['--']:
        args.arguments = args.arguments[1:]
    try:
        if os.name != 'nt':
            return linux_main(args)
        if shutil.which('wsl.exe') is None:
            raise RuntimeError('未找到 WSL。请在管理员 PowerShell 执行 wsl --install -d Ubuntu-24.04，再重启。')
        prefix = ['wsl.exe', '--distribution', args.distro, '--exec']
        script = subprocess.check_output([*prefix, 'wslpath', '-a', Path(__file__).resolve().as_posix()],
                                         text=True, encoding='utf-8').strip()
        options = ['--inside-wsl']
        if args.state_root:
            options += ['--state-root', args.state_root]
        return subprocess.call([*prefix, 'python3', script, *options, args.action, *args.arguments])
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'WSL 仿真启动失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

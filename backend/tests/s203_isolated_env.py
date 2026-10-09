#!/usr/bin/env python3
"""Explicit disposable S2-03 target guard and subprocess launcher."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = ('init_pg_database.py', 'admin/migrate_rbac_core.py', 'admin/migrate_knowledge_base_tables.py', 'admin/migrate_jobs_table.py', 'admin/migrate_qa_traces_table.py', 'admin/migrate_logs_table.py', 'admin/migrate_audit_scope_columns.py', 'admin/migrate_job_worker_lease.py', 'admin/migrate_jobs_targets_hash.py')
AUTHORIZATION = 'S203B-ISOLATED-AUTHORIZED'

class GuardError(RuntimeError):
    pass

def state_path(path: Path) -> Path:
    if not path.is_absolute():
        raise GuardError('ABSOLUTE_STATE_REQUIRED')
    resolved = path.resolve()
    if resolved.is_relative_to(BACKEND.parent.resolve()):
        raise GuardError('STATE_PATH_INSIDE_REPOSITORY')
    if path.is_symlink() or path.name != 'state.json':
        raise GuardError('STATE_PATH_INVALID')
    return resolved

def default_state_path(run_id: str) -> Path:
    if not re.fullmatch(r'[a-z0-9]{8,24}', run_id):
        raise GuardError('RUN_ID_INVALID')
    return state_path(Path.home() / '.graphinsight-s203' / run_id / 'state.json')

def wait_ready(state: dict, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuardError('PG_READY_TIMEOUT_RUN_TEARDOWN')
        try:
            result = command(['docker', 'exec', state['container'], 'pg_isready', '-h', '127.0.0.1', '-p', '5432', '-U', 's203', '-d', 'gi_s203_' + state['run_id']], timeout=min(5, remaining))
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))

def guard(dsn: str, run_id: str, port: int) -> None:
    if not re.fullmatch(r'[a-z0-9]{8,24}', run_id):
        raise GuardError('RUN_ID_INVALID')
    if not 1024 <= port <= 65535 or port in {5432, 5434, 55432, 55433}:
        raise GuardError('PORT_FORBIDDEN')
    try:
        url = urlsplit(dsn)
        valid = url.scheme in {'postgresql', 'postgresql+psycopg2', 'postgresql+psycopg'} and url.hostname == '127.0.0.1' and url.port == port and url.path == '/gi_s203_' + run_id and bool(url.username) and bool(url.password) and not url.query and not url.fragment
    except ValueError:
        valid = False
    if not valid:
        raise GuardError('DSN_TARGET_MISMATCH')

def redacted(dsn: str) -> str:
    url = urlsplit(dsn)
    return f'{url.scheme}://{url.username}:***@{url.hostname}:{url.port}{url.path}'

def write_private(path: Path, content: str) -> None:
    with path.open('x', encoding='utf-8') as stream:
        stream.write(content)
    os.chmod(path, 0o600)

def read_state(path: Path) -> dict:
    if path.is_symlink():
        raise GuardError('STATE_SYMLINK')
    state = json.loads(path.read_text(encoding='utf-8'))
    guard(state['dsn'], state['run_id'], state['pg_port'])
    if state['container'] != 'gi-s203-' + state['run_id'] or state['volume'] != 'gi-s203-volume-' + state['run_id']:
        raise GuardError('RESOURCE_NAME_MISMATCH')
    if state['image'] != 'postgres:16-alpine':
        raise GuardError('IMAGE_MISMATCH')
    return state

def environment(state: dict, env_file: Path) -> dict:
    if not env_file.is_file() or env_file.is_symlink():
        raise GuardError('ENV_FILE_MISSING')
    # 必须继承完整父环境：Windows 下 CPython 导入 asyncio/_overlapped 依赖一批系统变量
    # （白名单裁剪会触发 WinError 10106），且解释器自身依赖 PATH/TEMP 之外的多项设置。
    # 隔离性由下方显式覆盖的隔离键、GRAPHINSIGHT_BACKEND_ENV_FILE 指向的临时 env 文件，
    # 以及子进程内的 engine/current_database 双重断言共同保证，不依赖变量裁剪。
    env = os.environ.copy()
    env.update({'GRAPHINSIGHT_BACKEND_ENV_FILE': str(env_file.resolve()), 'ADMIN_DATABASE_URL': state['dsn'], 'ADMIN_SECRET_KEY': state['secret'], 'SEED_ADMIN_PASSWORD': state['password'], 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'SEED_KB_TENANT': 's203-tenant', 'SEED_KB_PROJECT': 's203-project', 'SEED_VERIFY_BASE_URL': '', 'NEO4J_CONFIG_SOURCE': 'env', 'RBAC_AUTHZ_MODE': 'go_db', 'S203_CHILD_AUTHORIZED': AUTHORIZATION})
    return env

def command(args: list[str], env: dict | None = None, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, cwd=BACKEND, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)

def docker_json(args: list[str]) -> list:
    result = command(['docker', *args])
    if result.returncode:
        raise GuardError('DOCKER_INSPECTION_FAILED')
    return json.loads(result.stdout)

def owned(state: dict) -> None:
    container = docker_json(['inspect', state['container']])[0]
    volume = docker_json(['volume', 'inspect', state['volume']])[0]
    label = {'graphinsight.s203.run': state['run_id']}
    if any(obj.get('Labels', {}) != label for obj in [volume]) or container.get('Config', {}).get('Labels') != label:
        raise GuardError('RESOURCE_OWNERSHIP_MISMATCH')
    bindings = container.get('HostConfig', {}).get('PortBindings', {}).get('5432/tcp', [])
    if bindings != [{'HostIp': '127.0.0.1', 'HostPort': str(state['pg_port'])}]:
        raise GuardError('PORT_BINDING_MISMATCH')
    if not any(m.get('Name') == state['volume'] and m.get('Destination') == '/var/lib/postgresql/data' for m in container.get('Mounts', [])):
        raise GuardError('VOLUME_MISMATCH')
    print(json.dumps({'container': state['container'], 'volume': state['volume'], 'created': container['Created'], 'status': container['State']['Status'], 'port': state['pg_port']}))

def child(state: dict, env_file: Path, script: str, token: bool = False) -> None:
    guard(state['dsn'], state['run_id'], state['pg_port'])
    result = command([sys.executable, str(Path(__file__).resolve()), '_child', '--state', str(env_file.parent / 'state.json'), '--script', script], environment(state, env_file))
    if token and result.returncode == 0:
        value = result.stdout.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', value):
            raise GuardError('TOKEN_OUTPUT_INVALID')
        write_private(env_file.parent / 'admin-token.txt', value)
    print(json.dumps({'entry': script, 'exit_code': result.returncode, 'output': 'WITHHELD'}))
    if result.returncode:
        raise GuardError('CHILD_FAILED')

def child_main(state: dict, script: str, env_file: Path) -> int:
    guard(state['dsn'], state['run_id'], state['pg_port'])
    if script not in (*MIGRATIONS, 'scripts/seed_e2e_local_stack.py', 'tests/issue_admin_token.py'):
        raise GuardError('SCRIPT_NOT_ALLOWED')
    if not env_file.is_file() or env_file.is_symlink():
        raise GuardError('ENV_FILE_MISSING')
    import dotenv
    import dotenv.main as dm
    original = dm.load_dotenv
    def locked_load(*args, **kwargs):
        if kwargs.get('stream') is not None:
            raise GuardError('DOTENV_STREAM_FORBIDDEN')
        return original(str(env_file), override=True)
    for module in (dotenv, dm):
        module.load_dotenv = locked_load
        module.find_dotenv = lambda *args, **kwargs: str(env_file)
    os.environ.pop('PYTHON_DOTENV_DISABLED', None)
    locked_load()
    if os.environ.get('ADMIN_DATABASE_URL') != state['dsn'] or os.environ.get('ADMIN_SECRET_KEY') != state['secret']:
        raise GuardError('ENV_CONTENT_MISMATCH')
    sys.path.insert(0, str(BACKEND))
    from admin.database import engine
    from sqlalchemy.engine import make_url
    expected = make_url(state['dsn'])
    identity = lambda url: (url.host, url.port, url.username, url.database)
    if engine.dialect.name != 'postgresql' or identity(engine.url) != identity(expected):
        raise GuardError('ENGINE_TARGET_MISMATCH')
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        if conn.execute(text('SELECT current_database()')).scalar() != 'gi_s203_' + state['run_id']:
            raise GuardError('SERVER_DATABASE_MISMATCH')
    sys.argv = [str(BACKEND / script)]
    if script == 'admin/migrate_logs_table.py':
        namespace = runpy.run_path(str(BACKEND / script), run_name='s203_logs')
        if namespace['migrate_logs_table']() is not True:
            return 1
        required = {'resource_id', 'user_agent', 'status', 'error_message'}
        return 0 if required <= {c['name'] for c in inspect(engine).get_columns('admin_logs')} else 1
    try:
        runpy.run_path(str(BACKEND / script), run_name='__main__')
    except SystemExit as error:
        if error.code not in (0, None):
            raise
    if script in MIGRATIONS:
        required_tables = {'admin_users', 'admin_logs'}
        if script != 'init_pg_database.py':
            required_tables.add('admin_roles')
        if script in MIGRATIONS[2:]: required_tables.update({'knowledge_bases', 'knowledge_base_documents'})
        if script in MIGRATIONS[3:]: required_tables.add('admin_jobs')
        if script in MIGRATIONS[4:]: required_tables.add('admin_qa_traces')
        if not required_tables <= set(inspect(engine).get_table_names()):
            raise GuardError('MIGRATION_POSTCHECK_FAILED')
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['guard-check', 'up', 'migrate', 'seed', 'token', 'teardown', '_child'])
    parser.add_argument('--state', type=Path)
    parser.add_argument('--run-id', default='')
    parser.add_argument('--pg-port', type=int, default=5544)
    parser.add_argument('--dsn', default='')
    parser.add_argument('--script', default='')
    parser.add_argument('--authorize', default='')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.state is not None:
            args.state = state_path(args.state)
        elif args.action == 'up':
            args.run_id = args.run_id or secrets.token_hex(6)
            args.state = default_state_path(args.run_id)
        if args.action == 'guard-check':
            guard(args.dsn, args.run_id, args.pg_port)
            print(json.dumps({'guard': 'PASS', 'ddl_calls': 0, 'dsn': redacted(args.dsn)}))
            return 0
        if args.authorize and args.authorize != AUTHORIZATION:
            raise GuardError('AUTHORIZATION_INVALID')
        if args.dry_run:
            if args.dsn:
                guard(args.dsn, args.run_id, args.pg_port)
            print(json.dumps({'action': args.action, 'plan': list(MIGRATIONS), 'resources_created': 0, 'ddl_calls': 0, 'execution': 'NOT_EXECUTED'}))
            return 0
        if args.action == '_child':
            if os.environ.get('S203_CHILD_AUTHORIZED') != AUTHORIZATION:
                raise GuardError('CHILD_NOT_AUTHORIZED')
        elif args.authorize != AUTHORIZATION:
            raise GuardError('S203B_AUTHORIZATION_REQUIRED')
        if not args.state or not args.state.is_absolute():
            raise GuardError('ABSOLUTE_STATE_REQUIRED')
        if args.action == 'up':
            if args.state.exists() or args.state.is_symlink():
                raise GuardError('NEW_STATE_REQUIRED')
            run_id = args.run_id or secrets.token_hex(6)
            password = secrets.token_urlsafe(32)
            state = {'run_id': run_id, 'pg_port': args.pg_port, 'container': 'gi-s203-' + run_id, 'volume': 'gi-s203-volume-' + run_id, 'image': 'postgres:16-alpine', 'password': password, 'secret': secrets.token_urlsafe(48), 'dsn': f'postgresql://s203:{password}@127.0.0.1:{args.pg_port}/gi_s203_{run_id}'}
            guard(state['dsn'], run_id, args.pg_port)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', args.pg_port))
            for query in [['container', 'ls', '-a', '--filter', 'name=^/' + state['container'] + '$', '--format', '{{.Names}}'], ['volume', 'ls', '--filter', 'name=^' + state['volume'] + '$', '--format', '{{.Name}}']]:
                check = command(['docker', *query])
                if check.returncode or check.stdout.strip():
                    raise GuardError('RESOURCE_ALREADY_EXISTS_OR_UNKNOWN')
            args.state.parent.mkdir(parents=True, exist_ok=True)
            write_private(args.state, json.dumps(state))
            env_file = args.state.parent / 'isolated.env'
            if args.state.name != 'state.json':
                raise GuardError('STATE_NAME_REQUIRED')
            write_private(env_file, '\n'.join(f'{key}={value}' for key, value in environment_values(state).items()) + '\n')
            label = 'graphinsight.s203.run=' + run_id
            result = command(['docker', 'volume', 'create', '--label', label, state['volume']])
            if result.returncode: raise GuardError('VOLUME_CREATE_FAILED')
            env = os.environ.copy(); env['POSTGRES_PASSWORD'] = password
            result = command(['docker', 'run', '-d', '--name', state['container'], '--label', label, '-p', f'127.0.0.1:{args.pg_port}:5432', '-v', state['volume'] + ':/var/lib/postgresql/data', '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_USER=s203', '-e', 'POSTGRES_DB=gi_s203_' + run_id, state['image']], env)
            if result.returncode: raise GuardError('CONTAINER_CREATE_FAILED')
            owned(state)
            wait_ready(state)
            print(json.dumps({'ready': True, 'state_file': str(args.state)}))
            return 0
        state = read_state(args.state)
        env_file = args.state.parent / 'isolated.env'
        if args.action == 'teardown':
            container_names = command(['docker', 'container', 'ls', '-a', '--filter', 'name=^/' + state['container'] + '$', '--format', '{{.Names}}'])
            volume_names = command(['docker', 'volume', 'ls', '--filter', 'name=^' + state['volume'] + '$', '--format', '{{.Name}}'])
            if container_names.returncode or volume_names.returncode: raise GuardError('RESOURCE_ENUMERATION_FAILED')
            if container_names.stdout.strip():
                obj = docker_json(['inspect', state['container']])[0]
                if obj.get('Config', {}).get('Labels') != {'graphinsight.s203.run': state['run_id']}: raise GuardError('RESOURCE_OWNERSHIP_MISMATCH')
                if command(['docker', 'rm', '-f', state['container']]).returncode: raise GuardError('TEARDOWN_FAILED')
            if volume_names.stdout.strip():
                obj = docker_json(['volume', 'inspect', state['volume']])[0]
                if obj.get('Labels') != {'graphinsight.s203.run': state['run_id']}: raise GuardError('RESOURCE_OWNERSHIP_MISMATCH')
                if command(['docker', 'volume', 'rm', state['volume']]).returncode: raise GuardError('TEARDOWN_FAILED')
            for query in [['container', 'ls', '-a', '--filter', 'name=^/' + state['container'] + '$', '--format', '{{.Names}}'], ['volume', 'ls', '--filter', 'name=^' + state['volume'] + '$', '--format', '{{.Name}}']]:
                result = command(['docker', *query])
                if result.returncode or result.stdout.strip(): raise GuardError('RESIDUE_OR_UNVERIFIED')
            print('RESIDUE containers=0 volumes=0')
            return 0
        if args.action == '_child':
            if os.environ.get('GRAPHINSIGHT_BACKEND_ENV_FILE') != str(env_file.resolve()): raise GuardError('CHILD_ENV_NOT_PINNED')
            return child_main(state, args.script, env_file)
        owned(state)
        if args.action == 'migrate':
            for script in MIGRATIONS: child(state, env_file, script)
        elif args.action in {'seed', 'token'}:
            child(state, env_file, 'scripts/seed_e2e_local_stack.py' if args.action == 'seed' else 'tests/issue_admin_token.py', args.action == 'token')
        return 0
    except (GuardError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'result': 'FAILED', 'reason': str(error) if isinstance(error, GuardError) else type(error).__name__, 'details': 'WITHHELD'}), file=sys.stderr)
        return 2

def environment_values(state: dict) -> dict:
    return {'ADMIN_DATABASE_URL': state['dsn'], 'ADMIN_SECRET_KEY': state['secret'], 'SEED_ADMIN_PASSWORD': state['password'], 'SEED_KB_TENANT': 's203-tenant', 'SEED_KB_PROJECT': 's203-project', 'SEED_VERIFY_BASE_URL': '', 'NEO4J_CONFIG_SOURCE': 'env', 'RBAC_AUTHZ_MODE': 'go_db', 'NEO4J_URI': 'bolt://127.0.0.1:1', 'NEO4J_PASSWORD': state['password'], 'MILVUS_URI': 'http://127.0.0.1:1', 'VECTOR_STORE_ENABLED': 'false', 'EMBEDDING_ENABLED': 'false'}

if __name__ == '__main__':
    raise SystemExit(main())

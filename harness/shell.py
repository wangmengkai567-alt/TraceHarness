"""Policy and one-use approval for fixed Linux Docker commands."""
from __future__ import annotations
import json
import math
import shlex
from .arithmetic import ArithmeticTools
from .command_policy import classify
from .contracts import ToolResult
from .files import FileTools, digest, FileError
from .sandbox import SandboxError, IMAGE
from .workspace import ordinary, relative_name


class ShellTools:
    def __init__(self, workspace, config, approve, emit, cancel, core=None, *, sandbox):
        self.workspace, self.config = str(workspace), config
        self.approve, self.emit, self.cancel = approve, emit, cancel
        self.core = core if core is not None else ArithmeticTools()
        self.sandbox = sandbox

    def schemas(self):
        return self.core.schemas() + [FileTools.schema(
            'run_command', 'Run Linux commands in /workspace inside the fixed offline Docker sandbox. '
            'Verified static read-only commands may run automatically. Scripts, writes and complex shell '
            'syntax need one-use user approval; forbidden requests are denied. No host shell is available.',
            {'command': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
             'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0,
                                 'maximum': self.config.shell_timeout_seconds}},
            ['command', 'timeout_seconds'])]

    def script_versions(self, command):
        versions = {}
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        for word in lexer:
            if not word.endswith(('.py', '.sh')) or any(c in word for c in '$*?[]{};|&\n'):
                continue
            try:
                relative_name(word)
            except FileError:
                # Image files are immutable under the approved image ID; expansions are not static references.
                continue
            try:
                path = ordinary(self.sandbox.workspace, word)
            except FileError as error:
                if str(error) != 'input_file_missing':
                    raise
                if not any(c.isspace() for c in word):
                    versions[word] = None
                continue
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError('script must be a bounded regular file')
            versions[word] = digest(path.read_bytes())
        return versions

    async def execute(self, call):
        if call.name != 'run_command':
            return await self.core.execute(call)
        try:
            args = json.loads(call.arguments)
            if not isinstance(args, dict) or set(args) != {'command', 'timeout_seconds'}:
                raise ValueError
            command, timeout = args['command'], args['timeout_seconds']
            if (not isinstance(command, str) or not command.strip() or len(command) > 8000 or '\0' in command
                    or type(timeout) not in (int, float) or not math.isfinite(timeout)
                    or not 0 < timeout <= self.config.shell_timeout_seconds):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return ToolResult(False, error='invalid_shell_arguments')
        try:
            policy = classify(command, self.sandbox.workspace)
        except Exception:
            policy = {'decision': 'deny', 'reason': 'policy_error', 'rule_version': '1'}
        self.emit('command_policy', call_id=call.id, command=command, **policy)
        if policy['decision'] == 'deny':
            return ToolResult(False, error='command_policy_denied', data=policy)
        try:
            image = await self.sandbox.preflight()
            versions = self.script_versions(command) if policy['decision'] == 'require_approval' else {}
        except (ValueError, OSError, FileError):
            return ToolResult(False, error='script_version_unavailable')
        except SandboxError as error:
            return ToolResult(False, error=str(error))
        spec = {'call_id': call.id, 'command': command, 'shell': '/bin/sh', 'image': IMAGE, 'image_id': image,
                'argv': policy.get('argv', ['/bin/sh', '-c', command]),
                'workspace': self.workspace, 'container_workspace': '/workspace', 'timeout_seconds': timeout,
                'policy': policy, 'script_versions': versions,
                'limits': {'cpus': 1, 'memory_mib': 512, 'pids': 64, 'tmp_mib': 64,
                           'network': 'none', 'root_read_only': True, 'user': '10001:10001',
                           'capabilities': [], 'no_new_privileges': True, 'workspace_disk_quota': None,
                           'output_bytes': self.config.max_output_chars // 2},
                'rules_readonly': '/workspace/AGENTS.md' if self.sandbox.rules else None}
        if policy['decision'] == 'require_approval':
            decision = await self.approve(spec)
            if decision != 'approve':
                return ToolResult(False, error='approval_' + decision)
            try:
                if self.script_versions(command) != versions:
                    return ToolResult(False, error='approval_script_changed')
            except (ValueError, OSError, FileError):
                return ToolResult(False, error='approval_script_changed')
        else:
            if classify(command, self.sandbox.workspace) != policy:
                return ToolResult(False, error='command_policy_changed')
            self.emit('command_auto_allowed', call_id=call.id, spec=spec)
        if self.cancel.is_set():
            return ToolResult(False, error='approval_cancelled')
        try:
            data = await self.sandbox.execute(spec, self.config.max_output_chars // 2)
        except SandboxError as error:
            return ToolResult(False, error='docker_execution_uncertain', data={'cause': str(error)})
        except OSError:
            return ToolResult(False, error='docker_start_or_control_error')
        result = ToolResult(not data['timed_out'] and data['exit_code'] == 0, data=data,
                            error='shell_timeout' if data['timed_out'] else
                            'shell_command_failed' if data['exit_code'] else None,
                            truncated=data['truncated'])
        while data['output'] and len(json.dumps(result.wire(), ensure_ascii=False)) > self.config.max_output_chars:
            data['output'] = data['output'][:len(data['output']) // 2]
            data['truncated'] = True
            result = ToolResult(result.ok, data=data, error=result.error, truncated=True)
        return result

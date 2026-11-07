"""Closed inline access to the canonical shared knowledge query, never a script runner."""
import json
import os
import re
import signal
import subprocess
import time


class UnavailableError(RuntimeError):
    pass


def validate(body):
    if (type(body) is not dict or set(body) != {'operation', 'arguments', 'source_revision'}
            or body['operation'] not in ('knowledge_query', 'master_file', 'loops_query', 'granola_history', 'relationship_importance', 'relationship_attention', 'triage_queue', 'style_apply', 'retune_scan')
            or type(body['arguments']) is not dict or set(body['arguments']) != {'argv'}):
        raise ValueError('knowledge read invalid')
    revision = body['source_revision']
    if body['operation'] == 'master_file':
        if revision is not None:
            raise ValueError('master read has no source revision')
    else:
        if (type(revision) is not str or len(revision) != 64
                or any(c not in '0123456789abcdef' for c in revision)):
            raise ValueError('knowledge read revision invalid')
    argv = body['arguments']['argv']
    if type(argv) is not list or len(argv) > 10 or any(type(value) is not str for value in argv):
        raise ValueError('knowledge arguments invalid')
    if body['operation'] == 'retune_scan':
        if argv:
            raise ValueError('retune arguments invalid')
        return argv
    if body['operation'] == 'style_apply':
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate style key')
                result[key] = value
            return result
        def constant(value):
            raise ValueError('nonfinite style input')
        if len(argv) != 1:
            raise ValueError('style arguments invalid')
        request = json.loads(argv[0], object_pairs_hook=unique, parse_constant=constant)
        if (type(request) is not dict or not {'recipient', 'channel'} <= set(request)
                or set(request) - {'recipient', 'channel', 'canonical_id', 'context'}):
            raise ValueError('style request invalid')
        recipient = request['recipient']
        if (type(recipient) is not str or not recipient or recipient.strip() != recipient or len(recipient) > 200
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF or c in '\u2028\u2029' for c in recipient)
                or request['channel'] not in ('email', 'imessage', 'whatsapp')):
            raise ValueError('style selector invalid')
        if ('canonical_id' in request and (type(request['canonical_id']) is not str
                or not re.fullmatch(r'c_[0-9a-f]{6,64}', request['canonical_id']))
                or 'context' in request and request['context'] not in ('work', 'personal', 'unknown')):
            raise ValueError('style context invalid')
        return argv
    if body['operation'] == 'granola_history':
        seen = set()
        index = 0
        while index < len(argv):
            flag = argv[index]
            if flag not in ('--days', '--meeting-id') or flag in seen or index + 1 >= len(argv):
                raise ValueError('Granola arguments invalid')
            seen.add(flag)
            value = argv[index + 1]
            index += 2
            if flag == '--days':
                if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 90 or str(int(value)) != value:
                    raise ValueError('Granola days invalid')
            elif (not value or value.strip() != value or len(value) > 512
                  or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
                raise ValueError('Granola meeting invalid')
        return argv
    if body['operation'] == 'relationship_attention':
        if argv:
            raise ValueError('attention arguments invalid')
        return ['--attention']
    if body['operation'] == 'relationship_importance':
        if argv == []:
            return argv
        if (len(argv) != 2 or argv[0] != '--person' or not argv[1]
                or argv[1].strip() != argv[1] or len(argv[1]) > 200
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in argv[1])):
            raise ValueError('relationship arguments invalid')
        return argv
    if body['operation'] == 'triage_queue':
        if argv:
            raise ValueError('triage arguments invalid')
        return argv
    if body['operation'] == 'loops_query':
        if argv not in ([], ['--parked']):
            raise ValueError('loops arguments invalid')
        return argv
    if body['operation'] == 'master_file':
        if argv != ['get']:
            raise ValueError('master arguments invalid')
        return argv
    if '--company' in argv:
        if (len(argv) not in (2, 4) or argv[0] != '--company' or not argv[1]
                or argv[1].strip() != argv[1] or len(argv[1]) > 200
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
                       or 0xD800 <= ord(c) <= 0xDFFF for c in argv[1])):
            raise ValueError('knowledge company selector invalid')
        if len(argv) == 4 and (argv[2] != '--company-page' or not re.fullmatch(r'first|[a-f0-9]{64}:[0-9]{1,9}', argv[3])):
            raise ValueError('knowledge company page invalid')
        return argv
    seen = set()
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in seen or flag not in ('--person', '--person-coverage', '--editable-person', '--facts-only', '--topic', '--relevant-days'):
            raise ValueError('knowledge flag invalid')
        seen.add(flag)
        index += 1
        if flag in ('--person-coverage', '--editable-person', '--facts-only'):
            continue
        if index == len(argv):
            raise ValueError('knowledge value missing')
        value = argv[index]
        index += 1
        if flag == '--relevant-days':
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 365 or str(int(value)) != value:
                raise ValueError('knowledge days invalid')
        elif (not value or value.strip() != value or len(value) > (200 if flag == '--person' else 500)
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError('knowledge selector invalid')
    if seen.intersection(('--person-coverage', '--editable-person')) and '--person' not in seen:
        raise ValueError('knowledge person required')
    if '--facts-only' in seen and (not {'--person', '--person-coverage'} <= seen
            or seen.intersection(('--editable-person', '--relevant-days'))):
        raise ValueError('facts-only requires person coverage without edit or cohort flags')
    return argv


def perform(body, *, script, executable, env, authority, disconnected=None, initial_authority=None):
    argv = validate(body)
    master = body['operation'] == 'master_file'
    # The master file contains owner-stated or owner-approved standing context. Connector
    # health and source revision do not authorize this fixed private read.
    # Only a receiver caller may supply its just-read snapshot. The post-read
    # check below always obtains fresh authority; client arguments cannot set this.
    before = None if master else (initial_authority if initial_authority is not None else authority())
    captured_revision = before.get('revision') if before is not None else None
    def guard(current):
        if (current.get('complete') is not True
                or current.get('revision') != captured_revision
                or current.get('revision') != body['source_revision']):
            raise UnavailableError('knowledge authority unavailable')
    if before is not None:
        guard(before)
    granola = body['operation'] == 'granola_history'
    if granola and 'granola' not in before.get('connector_sources', []):
        raise UnavailableError('Granola authority unavailable')
    if not script:
        raise UnavailableError('knowledge query unavailable')
    command = [executable, '-I', '-B', script, *argv]
    if body['operation'] == 'knowledge_query':
        # The public grammar stays closed; only this receiver requests the bounded projection.
        command.append('--interactive')
    if master:
        # Fixed module/operation only: render one original reader snapshot without read-side writes.
        code = ("import importlib.util,json,sys; "
                "s=importlib.util.spec_from_file_location('sotto_master_read',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "text=m.read(); prompt=m.render_for_chat_prompt(text); "
                "print(json.dumps({'text':text,'chars':len(text),'cap':m.MASTER_CHAR_CAP,'prompt':prompt}))")
        command = [executable, '-I', '-B', '-c', code, script]
    if body['operation'] == 'loops_query':
        permitted = (before.get('bridge_sources', []) + before.get('google_sources', [])
                     + before.get('connector_sources', []))
        code = ("import importlib.util,json,os,sys; "
                "s=importlib.util.spec_from_file_location('sotto_loops_read',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "print(json.dumps(m.query_permitted_rows(os.environ['SOTTO_DATA'],json.loads(sys.argv[2]),"
                "include_parked=sys.argv[3]=='1',include_ask=True)))")
        command = [executable, '-I', '-B', '-c', code, script, json.dumps(permitted), '1' if argv else '0']
    if body['operation'] == 'retune_scan':
        permitted = before.get('bridge_sources', []) + before.get('google_sources', []) + before.get('connector_sources', [])
        code = ("import importlib.util,json,os,sys; "
                "s=importlib.util.spec_from_file_location('sotto_retune_read',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "print(json.dumps(m.scan_permitted_rows(os.environ['SOTTO_DATA'],json.loads(sys.argv[2]))))")
        command = [executable, '-I', '-B', '-c', code, script, json.dumps(permitted)]
    if body['operation'] == 'style_apply':
        permitted = before.get('bridge_sources', []) + before.get('google_sources', []) + before.get('connector_sources', [])
        code = ("import importlib.util,json,os,sys; sys.path.insert(0,os.path.dirname(sys.argv[1])); "
                "s=importlib.util.spec_from_file_location('sotto_style_read',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "grants=set(json.loads(sys.argv[3])); "
                "print(json.dumps(m.apply(json.loads(sys.argv[2]),permitted=lambda source:source in grants)))")
        command = [executable, '-I', '-B', '-c', code, script, argv[0], json.dumps(permitted)]
    child_env = {**env, 'SOTTO_READ_ONLY': '1'}
    if body['operation'] == 'knowledge_query':
        # Historical obligations can depend on several sources. Use this read's
        # authority snapshot, not a saved setup file or an inherited environment.
        child_env['SOTTO_CAPTURED_SOURCES'] = json.dumps(sorted(set(
            before.get('bridge_sources', []) + before.get('google_sources', [])
            + before.get('connector_sources', []))))
    bounded_live = granola or body['operation'] == 'triage_queue'
    if bounded_live:
        if disconnected is not None and disconnected():
            raise UnavailableError('live read stopped')
        permitted = (before.get('bridge_sources', []) + before.get('google_sources', [])
                     + before.get('connector_sources', []))
        child_env['SOTTO_CAPTURED_SOURCES'] = json.dumps(permitted)
        triage = body['operation'] == 'triage_queue'
        process = subprocess.Popen(command, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=triage)
        deadline = time.monotonic() + 50
        try:
            while True:
                if time.monotonic() >= deadline or (disconnected is not None and disconnected()):
                    raise UnavailableError('live read stopped')
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, deadline - time.monotonic()))
                    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                if triage:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
    else:
        result = subprocess.run(command, env=child_env, capture_output=True, timeout=10)
    if result.returncode or len(result.stdout) > 64 * 1024:
        raise UnavailableError('knowledge query unavailable')
    def reject_constant(value):
        raise ValueError('non-finite knowledge value')
    try:
        value = json.loads(result.stdout, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise UnavailableError('knowledge result invalid') from None
    if type(value) is not dict or len(json.dumps(value, allow_nan=False).encode()) > 64 * 1024:
        raise UnavailableError('knowledge result invalid')
    if master and (set(value) != {'text', 'chars', 'cap', 'prompt'} or type(value['text']) is not str
            or type(value['chars']) is not int or value['chars'] != len(value['text'])
            or type(value['cap']) is not int or value['cap'] != 8000 or type(value['prompt']) is not str):
        raise UnavailableError('master result invalid')
    if not master:
        guard(authority())
    return value

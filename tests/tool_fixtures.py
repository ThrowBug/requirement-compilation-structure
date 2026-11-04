"""Author fake model tool responses for orchestration tests; no production compatibility layer."""

from infrastructure.utils.files import read_source


def propose_files(output, contents, **extra):
    if contents is None:
        return {**extra, 'tool_calls': None}
    staged, calls = {}, []
    for item in contents:
        path, content = item['path'], item['content']
        file = output / path
        before = item.get('old_content', staged.get(path, read_source(file) if file.resolve().is_relative_to(output.resolve()) and file.is_file() else None))
        call = {'tool': 'edit_file', 'path': path,
            'operation': 'create' if before is None else 'replace', 'new_content': content}
        if before is not None:
            call['old_content'] = before
            if 'old_content' not in item:
                if before == content:
                    continue
                # Author the same local edit expected from a real agent, rather
                # than submitting an obsolete whole-file response in fixtures.
                first = 0
                while first < min(len(before), len(content)) and before[first] == content[first]:
                    first += 1
                last = 0
                while last < min(len(before), len(content)) - first and before[-1-last] == content[-1-last]:
                    last += 1
                end = len(before) - last
                if first == len(before):
                    call.update(operation='append', old_content='', new_content=content[first:])
                elif end == 0:
                    call.update(operation='prepend', old_content='', new_content=content[:len(content)-last])
                else:
                    left, right = first, end
                    if left == right:
                        left = max(0, left - 1)
                        right = max(right, 1)
                    while True:
                        old = before[left:right]
                        at = before.find(old)
                        if old and before.find(old, at + 1) < 0:
                            break
                        if left > 0:
                            left -= 1
                        elif right < len(before):
                            right += 1
                        else:
                            break
                    call.update(old_content=before[left:right],
                        new_content=content[left:len(content)-(len(before)-right)])
        calls.append(call)
        staged[path] = content
    return {**extra, 'tool_calls': calls}

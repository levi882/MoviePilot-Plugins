"""Derive delivery directories from MP settings, bounded by worker read mounts."""
from collections.abc import Mapping
from pathlib import Path
import re


def value(row, key, default=None):
    result = row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)
    return getattr(result, 'value', result)


def resolve_directories(rules, bindings, queue):
    allowed = {str(Path(b['mp_root'])): b['id'] for b in bindings
               if Path(b.get('mp_root', '')).is_absolute() and re.fullmatch(r'[a-z0-9_-]{1,32}', b.get('id', ''))}
    choices = {}
    issues = []
    for rule in sorted(rules, key=lambda r: value(r, 'priority', 0) or 0):
        if value(rule, 'media_type') not in (None, '', '电影', 'Movie', 'movie'):
            continue
        download, library = value(rule, 'download_path'), value(rule, 'library_path')
        if not download or not library:
            continue
        name = value(rule, 'name') or str(library)
        if value(rule, 'storage', 'local') != 'local' or value(rule, 'library_storage', 'local') != 'local':
            issues.append(f'{name}：远程存储类型暂不处理')
            continue
        if not Path(download).is_absolute() or not Path(library).is_absolute():
            issues.append(f'{name}：目录必须为绝对路径')
            continue
        library, download = str(Path(library)), str(Path(download))
        if library not in allowed:
            issues.append(f'{name}：{library} 未挂载到处理服务，沿用 MP 原流程')
            continue
        choices.setdefault(library, []).append((name, download))
    mappings = []
    for library, entries in choices.items():
        if len({d for _, d in entries}) != 1:
            issues.append(f'{library}：多个下载目录对应同一电影库，无法唯一匹配，暂不重建')
            continue
        root_id = allowed[library]
        mappings.append({'id': root_id, 'name': entries[0][0], 'mp_root': library,
                         'download_root': entries[0][1], 'staging_root': str(Path(queue) / 'staging' / root_id)})
    return mappings, issues

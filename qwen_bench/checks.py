"""Conservative static checks; no SVG execution or artistic scoring."""
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

MAX_SVG_BYTES = 8 * 1024 * 1024


def inspect(path, width, height):
    path = Path(path)
    result = {'status': 'failed', 'expected_dimensions': [width, height],
              'visual_quality': 'not_observed', 'fixed_renderer': 'not_run'}
    if path.is_symlink() or not path.is_file():
        return dict(result, reason='Missing, symlink, or nonregular SVG')
    if path.stat().st_size > MAX_SVG_BYTES:
        return dict(result, reason='SVG exceeds 8 MiB safety limit')
    data = path.read_bytes()
    result['sha256'] = hashlib.sha256(data).hexdigest()
    if re.search(rb'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        return dict(result, reason='DTD/entity declarations are prohibited')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        return dict(result, reason=str(error), xml_valid=False)
    result['xml_valid'] = True
    result['svg_namespace'] = root.tag == '{http://www.w3.org/2000/svg}svg'
    def number(value):
        try: return float(str(value).removesuffix('px'))
        except (ValueError, TypeError): return None
    try: viewbox = [float(x) for x in re.split(r'[\s,]+', root.get('viewBox', '').strip())]
    except ValueError: viewbox = []
    result['canvas'] = [number(root.get('width')), number(root.get('height'))] == [width, height]
    result['viewbox'] = viewbox == [0, 0, width, height]
    ids = [e.get('id') for e in root.iter() if e.get('id')]
    result['unique_ids'] = len(ids) == len(set(ids))
    forbidden = {'image', 'feimage', 'script', 'foreignobject', 'animate', 'animatemotion',
                 'animatetransform', 'set', 'iframe', 'audio', 'video', 'discard'}
    problems, refs = [], []
    for element in root.iter():
        name = element.tag.rsplit('}', 1)[-1].lower()
        if name in forbidden or not element.tag.startswith('{http://www.w3.org/2000/svg}'):
            problems.append('prohibited/non-SVG element: '+name)
        css = element.text or '' if name == 'style' else ''
        for attribute, value in element.attrib.items():
            local = attribute.rsplit('}', 1)[-1].lower()
            if local.startswith('on') or local == 'base':
                problems.append('active/base attribute: '+local)
            if local == 'href':
                if value.startswith('#'): refs.append(value[1:])
                else: problems.append('external/nonfragment href')
            css += ' '+value
        if re.search(r'@import|data:image|@font-face', css, re.I) or '\\' in css:
            problems.append('external/escaped CSS unsupported')
        for url in re.findall(r'url\(\s*[\'\"]?([^\)\'\"]+)', css, re.I):
            if url.startswith('#'): refs.append(url[1:])
            else: problems.append('external CSS URL')
    result['safety_problems'] = sorted(set(problems))
    result['unresolved_references'] = sorted(set(refs)-set(ids))
    result['safe_preview'] = result['svg_namespace'] and not problems
    result['machine_gates_pass'] = (result['safe_preview'] and result['canvas'] and result['viewbox']
                                    and result['unique_ids'] and not result['unresolved_references'])
    result['status'] = 'verified' if result['machine_gates_pass'] else 'failed'
    return result

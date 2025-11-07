import re
import sys
from collections import defaultdict, deque

type_map = {
    "bool": "bool",
    "uint8": "uint8_t",
    "uint16": "uint16_t",
    "int16": "int16_t",
    "int32": "int32_t",
    "uint32": "uint32_t",
    "int64": "int64_t",
    "uint64": "uint64_t",
    "float32": "float",
    "float": "float",
    "Vector": "Vector3",
    "Vector2": "Vector2",
    "Vector2D": "Vector2",
    "Vector3": "Vector3",
    "Vector4": "Vector4",
    "QAngle": "Vector3",
    "Quaternion": "Vector4",
    "char": "char",
    "CUtlStringToken": "uint32_t",
    "CModelState": "void*",
}

array_map = {
    "Vector2D": "Vector2",
    "float[2]": "Vector2",
    "float[3]": "Vector3",
    "float[4]": "Vector4",
}

array_pattern = re.compile(r'([A-Za-z0-9_<>:]+)\[(\d+)\]')

pointer_size = 8  # x64

size_map = {
    "bool": 1,
    "int": 4,
    "uint8_t": 1,
    "int16_t": 2,
    "uint16_t": 2,
    "int32_t": 4,
    "uint32_t": 4,
    "float": 4,
    "int64_t": 8,
    "uint64_t": 8,
    "uintptr_t": pointer_size,
    "void*": pointer_size,
    "char": 1,
    "Vector2": 8,
    "Vector3": 12,
    "QAngle": 12,
    "Vector4": 16,
}

special_map = {
    "CHandle<C_BaseEntity>": ("uint32_t", 4),
    "CHandle<C_BaseModelEntity>": ("uint32_t", 4),
    "CUtlStringToken": ("uint32_t", 4),
    "GameTime_t": ("float", 4),
    "GameTick_t": ("int32_t", 4),
    "AttachmentHandle_t": ("uint8_t", 1),
}

array_pattern = re.compile(r'([A-Za-z0-9_<>:]+)\[(\d+)\]')

def normalize_type(ctype: str):
    ctype = ctype.strip()
    am = array_pattern.match(ctype)
    if am:
        base, count = am.groups()
        base = base.strip()
        count = int(count)
        if base in type_map:
            return (type_map[base], count)
        if base in type_map.values():
            return (base, count)
        return ("uint32_t", count)

    if ctype in array_map:
        return array_map[ctype]

    if ctype in type_map:
        return type_map[ctype]
    if ctype in type_map.values():
        return ctype

    if ctype in special_map:
        return special_map[ctype][0]

    return "uint32_t"


def parse_classes(text):
    class_pattern = re.compile(
        r'// Parent:\s*(\w+).*?namespace\s+(\w+)\s*\{([^}]*)\}',
        re.MULTILINE | re.DOTALL
    )
    offset_pattern = re.compile(
        r'constexpr std::ptrdiff_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+);\s*//\s*(.+)'
    )

    classes = []
    for match in class_pattern.finditer(text):
        parent = match.group(1)
        name = match.group(2)
        content = match.group(3).strip().splitlines()
        fields = []
        for line in content:
            fm = offset_pattern.search(line)
            if fm:
                fields.append(line.strip())
        classes.append((name, parent, fields))
    return classes


def order_classes(classes):
    name_to_class = {name: (name, parent, body) for name, parent, body in classes}
    deps = defaultdict(set)
    rev_deps = defaultdict(set)
    for name, parent, _ in classes:
        if parent and parent in name_to_class:
            deps[name].add(parent)
            rev_deps[parent].add(name)
    indegree = {name: len(deps[name]) for name, _, _ in classes}
    queue = deque([n for n, d in indegree.items() if d == 0])
    ordered = []
    while queue:
        name = queue.popleft()
        _, parent, body = name_to_class[name]
        if parent not in name_to_class:
            parent = None
        ordered.append((name, parent, body))
        for dep in rev_deps[name]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)
    return ordered


def compute_class_sizes(ordered):
    sizes = {}
    offset_re = re.compile(r'constexpr std::ptrdiff_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+);\s*//\s*(.+)')
    for name, parent, fields in ordered:
        last_offset = 0
        for line in fields:
            m = offset_re.search(line)
            if not m: 
                continue
            _, offset_hex, ctype = m.groups()
            offset = int(offset_hex, 16)
            nt = normalize_type(ctype)

            if isinstance(nt, tuple):
                base_type, count = nt
                elem_size = size_map.get(base_type, 4)
                size = elem_size * count
            else:
                size = size_map.get(nt, 4)

            end = offset + size
            if end > last_offset:
                last_offset = end

        parent_size = sizes.get(parent, 0)
        sizes[name] = max(last_offset, parent_size)
    return sizes

def convert_file(text):
    classes = parse_classes(text)
    ordered = order_classes(classes)
    sizes = compute_class_sizes(ordered)

    output = []
    output.append("#pragma once\n")
    output.append("#include <cstdint>\n")
    output.append("#include <cstddef>\n\n")

    output.append("struct Vector2 { float x, y; };\n")
    output.append("struct Vector3 { float x, y, z; };\n")
    output.append("struct Vector4 { float x, y, z, w; };\n")
    output.append("using QAngle = Vector3;\n\n")

    for name, _, _ in ordered:
        output.append(f"class {name};\n")
    output.append("\n")

    offset_re = re.compile(r'constexpr std::ptrdiff_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+);\s*//\s*(.+)')

    for name, parent, fields in ordered:
        parent_decl = f" : public {parent}" if parent else ""
        output.append(f"class {name}{parent_decl}\n{{\npublic:\n")

        parent_size = sizes.get(parent, 0) if parent else 0
        last_offset = parent_size

        for line in fields:
            m = offset_re.search(line)
            if not m: 
                continue
            var, offset_hex, ctype = m.groups()
            offset = int(offset_hex, 16)

            if offset < last_offset:
                continue

            if offset > last_offset:
                pad_size = offset - last_offset
                output.append(f"    char pad_{last_offset:04X}[{pad_size}];\n")
                last_offset = offset

            nt = normalize_type(ctype)

            if isinstance(nt, tuple):
                base_type, count = nt
                elem_size = size_map.get(base_type, 4)
                size = elem_size * count
                output.append(f"    {base_type} {var}[{count}]; // 0x{offset:04X} {ctype}\n")
                last_offset = offset + size
            else:
                size = size_map.get(nt, 4)
                output.append(f"    {nt} {var}; // 0x{offset:04X} {ctype}\n")
                last_offset = offset + size

        output.append("};\n\n")

    return "".join(output)


def main():
    if len(sys.argv) < 3:
        print("Uso: python main.py <input.h> <output.h>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file, "r", encoding="utf-8") as f:
        text = f.read()

    result = convert_file(text)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"[OK] Arquivo convertido salvo em {output_file}")

if __name__ == "__main__":
    main()


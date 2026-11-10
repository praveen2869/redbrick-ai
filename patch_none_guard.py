with open("src/pipeline.py", "r", encoding="utf-8") as f:
    content = f.read()

target = '''    excluded_names = set()
    if exclude_tenants:
        for item in str(exclude_tenants).split(","):
            cleaned = item.strip().lower()
            if cleaned:
                excluded_names.add(cleaned)'''

replacement = '''    excluded_names = set()
    if exclude_tenants:
        for item in str(exclude_tenants).split(","):
            cleaned = item.strip().lower()
            if cleaned and cleaned not in ("none", "null", "empty", "false", '""', "''"):
                excluded_names.add(cleaned)'''

content = content.replace(target.replace("\r\n", "\n"), replacement.replace("\r\n", "\n"))
content = content.replace(target.replace("\n", "\r\n"), replacement.replace("\n", "\r\n"))

with open("src/pipeline.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated src/pipeline.py with none/empty guard")


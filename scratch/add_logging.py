
file_path = 'C:/Users/pizzalover/Documents/antigravity/serene-hypatia/web/routes/pages.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

target = '    import web.app as _app'
replacement = '    import web.app as _app\n    logger.debug(f"DEBUG: server_page received time_range={time_range}")'

new_content = content.replace(target, replacement)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

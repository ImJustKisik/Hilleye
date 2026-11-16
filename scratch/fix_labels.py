
file_path = 'C:/Users/pizzalover/Documents/antigravity/serene-hypatia/web/templates/server.html'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

target = '          {% for segment in server.chart_24h.segments %}'
replacement = '          <text x="0" y="20" font-size="10" fill="#888">{{ server.chart_24h.max_players }}</text>\n          <text x="0" y="110" font-size="10" fill="#888">{{ server.chart_24h.min_players }}</text>\n          {% for segment in server.chart_24h.segments %}'

new_content = content.replace(target, replacement)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

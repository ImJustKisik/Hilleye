
import re

file_path = "C:/Users/pizzalover/Documents/antigravity/serene-hypatia/core/db/dossiers.py"
with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
in_async_with = False
for line in lines:
    if "async with conn.cursor() as cur:" in line:
        in_async_with = True
        new_lines.append(line)
        continue
        
    if in_async_with:
        # Check if the line is still in the function (based on indentation, 
        # get_server_details is level 0, inside it is 4, inside async with is 8)
        # Actually the function def is level 0, body level 4.
        # This line is complex because of nested blocks.
        
        # Let's just fix the specific lines 605-647
        # Based on my manual check: 
        # Lines 605-625 and 627-647 should be at 8 spaces.
        
        # Simple heuristic: if it has 4 spaces and is part of the code I touched, make it 8 spaces.
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
             # For comments, maybe just keep them as they are or align them
             if line.startswith("    #"):
                 new_lines.append("        " + stripped)
                 continue
        
        if line.startswith("    ") and not line.startswith("        ") and len(line.strip()) > 0:
             # If it starts with 4 spaces but not 8 (and not empty)
             new_lines.append("        " + stripped)
        else:
             new_lines.append(line)
    else:
        new_lines.append(line)

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

content = open('gemini_dictate.py', encoding='utf-8').read()
lines = content.split('\n')
for i,l in enumerate(lines,1):
    if 'btn.config' in l or 'btn.grid' in l:
        print(i, l[:100])

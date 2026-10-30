"""Remove UI scaffolding while preserving requests and user annotations."""
import json
import re

def semantic_text(text):
    text=text or ''
    text=re.sub(r'<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>','',text,flags=re.S)
    text=re.sub(r'^# Response annotations:.*?(?=<response-annotations>)','',text,flags=re.S)
    annotations=[]
    def extract(match):
        try:
            rows=json.loads(match.group(1))
            for row in rows:
                if isinstance(row,dict) and row.get('annotation'):
                    annotations.append('引用：'+str(row.get('text',''))+'\n用户批注：'+str(row['annotation']))
            return ''
        except (ValueError,TypeError):return match.group(0)
    text=re.sub(r'<response-annotations>(.*?)</response-annotations>',extract,text,flags=re.S)
    text=re.sub(r'^\s*## My request:\s*','',text,flags=re.M).strip()
    return '\n\n'.join(annotations+([text] if text else []))

"""Check pagination, references, evidence assets and render every PDF page for review."""
from pathlib import Path
import json,re
import pymupdf as fitz
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
D=fitz.open(ROOT/'VAS-User-Guide.pdf');pages={};issues=[]
for i,page in enumerate(D):
 for block in page.get_text('dict')['blocks']:
  if 'lines' not in block:continue
  for line in block['lines']:
   spans=line['spans'];s=''.join(x['text'] for x in spans)
   if max((x['size'] for x in spans),default=0)<20:continue
   m=re.match(r'^(W\d{2}[A-Z]?)\s+',s)
   if m:pages.setdefault(m[1],i+1)
   m=re.match(r'^([1-8])\.\s+',s)
   if m:pages.setdefault('S'+m[1],i+1)
 for b in page.get_text('blocks'):
  if b[0]<20 or b[1]<10 or b[2]>page.rect.width-15 or b[3]>page.rect.height-10:issues.append({'page':i+1,'problem':'Text outside page bounds'})
 for im in page.get_image_info():
  x0,y0,x1,y1=im['bbox']
  if x0<20 or y0<20 or x1>page.rect.width-20 or y1>page.rect.height-20:issues.append({'page':i+1,'problem':'Image outside page bounds'})
 for link in page.get_links():
  if link.get('kind')==fitz.LINK_GOTO and not 0<=link.get('page',-1)<len(D):issues.append({'page':i+1,'problem':'Invalid internal link'})
ws=json.loads((ROOT/'source/workflows.json').read_text());fig=json.loads((ROOT/'source/figure-index.json').read_text())
for w in ws:
 if w['id'] not in pages:issues.append({'workflow':w['id'],'problem':'Missing destination heading'})
 numbers=set()
 for name in w['figures']:
  for folder,ext in [('screenshots','png'),('annotated','png'),('annotations','svg'),('annotations','json')]:
   if not (ROOT/folder/(name+'.'+ext)).exists():issues.append({'figure':name,'problem':'Missing '+folder})
  numbers.update(m['number'] for m in fig[name]['marks'])
 missing=set(range(1,len(w['steps'])+1))-numbers
 if missing:issues.append({'workflow':w['id'],'problem':'Missing arrows','steps':sorted(missing)})
old=json.loads((ROOT/'source/page-map.json').read_text()) if (ROOT/'source/page-map.json').exists() else {}
(ROOT/'source/page-map.json').write_text(json.dumps(pages,indent=2))
report={'pages':len(D),'workflows':len(ws),'figures':len(fig),'capabilities':len(json.loads((ROOT/'source/coverage.json').read_text())),'issues':issues,'page_map_changed':old!=pages,'internal_links':sum(len(p.get_links()) for p in D)}
(ROOT/'source/layout-check.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));print(pages)
# Review copies are deliberately outside the distribution folder.
OUT=Path('/tmp/vas-guide-page-review');OUT.mkdir(exist_ok=True)
for i,page in enumerate(D):page.get_pixmap(matrix=fitz.Matrix(1.1,1.1),alpha=False).save(OUT/f'page-{i+1:03d}.png')
for start in range(0,len(D),12):
 sheet=Image.new('RGB',(1500,1600),'#dbe3e9');draw=ImageDraw.Draw(sheet)
 for j in range(min(12,len(D)-start)):
  im=Image.open(OUT/f'page-{start+j+1:03d}.png');im.thumbnail((490,370));x=j%3*500+(500-im.width)//2;y=j//3*400+23;sheet.paste(im,(x,y));draw.text((j%3*500+8,j//3*400+4),f'Page {start+j+1}',fill='black')
 sheet.save(OUT/f'sheet-{start//12+1:02d}.jpg',quality=88)
print('Rendered every PDF page to',OUT)

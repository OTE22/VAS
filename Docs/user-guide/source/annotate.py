"""Render editable SVG arrows over sanitized browser screenshots; never synthesize UI."""
from pathlib import Path
import json,math,html,base64
from PIL import Image
import cairosvg
ROOT=Path(__file__).resolve().parents[1]
ROOT.joinpath('annotated').mkdir(exist_ok=True)
ROOT.joinpath('screenshots/focus').mkdir(exist_ok=True)
workflows=json.loads(ROOT.joinpath('source/workflows.json').read_text())
used=list(dict.fromkeys(f for w in workflows for f in w['figures']))
index={}
for name in used:
 source=ROOT/'screenshots'/f'{name}.png';meta=ROOT/'annotations'/f'{name}.json'
 if not source.exists():raise RuntimeError('Missing verified screenshot '+name)
 info=json.loads(meta.read_text());im=Image.open(source).convert('RGB');W,H=im.size
 marks=[]
 for mark in info['marks']:
  x,y,w,h=mark['rect'];l=max(0,x);t=max(0,y);r=min(W,x+w);b=min(H,y+h)
  if r<=l or b<=t:raise RuntimeError(f'Off-screen target {name} step {mark["number"]}')
  marks.append({**mark,'rect':[l,t,r-l,b-t]})
 if marks:
  l=max(0,min(m['rect'][0] for m in marks)-100);t=max(0,min(m['rect'][1] for m in marks)-85)
  r=min(W,max(m['rect'][0]+m['rect'][2] for m in marks)+85);b=min(H,max(m['rect'][1]+m['rect'][3] for m in marks)+70)
  # Context minimum without shrinking long/narrow dialogs into postage stamps.
  if r-l<500:l=max(0,(l+r)/2-250);r=min(W,l+500)
  if b-t<220:t=max(0,(t+b)/2-110);b=min(H,t+220)
 else:l,t,r,b=16,75,W-16,H-20
 crop=tuple(map(int,info.get("crop_override",(l,t,r,b))));im=im.crop(crop);cw,ch=im.size
 focus=ROOT/'screenshots/focus'/f'{name}.png';im.save(focus)
 pad=65;width=cw+pad*2;height=ch+pad*2
 rects=[[m['rect'][0]-crop[0]+pad,m['rect'][1]-crop[1]+pad,m['rect'][2],m['rect'][3]] for m in marks]
 occupied=[]
 svg=[f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',f'<title>{html.escape(name)} — numbered VAS instructions</title>', '<desc>Original sanitized browser capture with editable instruction numbers and arrows. Orange circles are guide steps, not application controls.</desc>',f'<rect width="{width}" height="{height}" fill="#f1f5f8"/>',f'<image x="{pad}" y="{pad}" width="{cw}" height="{ch}" xlink:href="data:image/png;base64,{base64.b64encode(focus.read_bytes()).decode()}"/>']
 positions=[]
 for m,rect in zip(marks,rects):
  x,y,w,h=rect;cx=x+w/2;cy=y+h/2;rad=21
  candidates=[(x-42,cy),(x+w+42,cy),(cx,y-43),(cx,y+h+43),(pad/2,cy),(width-pad/2,cy),(cx,pad/2),(cx,height-pad/2)]
  def valid(p):
   px,py=p
   if px<rad+3 or py<rad+3 or px>width-rad-3 or py>height-rad-3:return False
   if any(abs(px-a)<rad*2+7 and abs(py-b)<rad*2+7 for a,b in occupied):return False
   return not any(px+rad+7>rx and px-rad-7<rx+rw and py+rad+7>ry and py-rad-7<ry+rh for rx,ry,rw,rh in rects)
  pos=next((p for p in candidates if valid(p)),None)
  if pos is None:
   pos=next(((pad/2,j) for j in range(30,height-20,48) if valid((pad/2,j))),None)
  if pos is None:raise RuntimeError('No clear annotation position: '+name)
  px,py=pos;occupied.append(pos)
  tx=min(max(px,x+3),x+w-3);ty=min(max(py,y+3),y+h-3)
  if x<px<x+w:ty=y-3 if py<y else y+h+3
  else:tx=x-3 if px<x else x+w+3
  dx=tx-px;dy=ty-py;dist=math.hypot(dx,dy);ux=dx/dist;uy=dy/dist
  sx=px+ux*(rad+2);sy=py+uy*(rad+2)
  ax=tx-ux*12;ay=ty-uy*12
  points=f'{tx:.1f},{ty:.1f} {ax-uy*6:.1f},{ay+ux*6:.1f} {ax+uy*6:.1f},{ay-ux*6:.1f}'
  svg += [f'<g id="step-{m["number"]}"><title>Step {m["number"]}: {html.escape(m["selector"])}</title>',f'<path d="M {sx:.1f} {sy:.1f} L {tx:.1f} {ty:.1f}" fill="none" stroke="white" stroke-width="6"/>',f'<path d="M {sx:.1f} {sy:.1f} L {tx:.1f} {ty:.1f}" fill="none" stroke="#e46b2c" stroke-width="3"/>',f'<polygon points="{points}" fill="#e46b2c" stroke="white" stroke-width="1"/>',f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{rad}" fill="#132c42" stroke="#e46b2c" stroke-width="3"/>',f'<text x="{px:.1f}" y="{py+7:.1f}" text-anchor="middle" font-family="DejaVu Sans" font-size="22" font-weight="bold" fill="white">{m["number"]}</text></g>']
  positions.append({'number':m['number'],'badge':[px,py],'arrow_tip':[tx,ty],'target':rect})
 svg.append('</svg>');svgfile=ROOT/'annotations'/f'{name}.svg';svgfile.write_text('\n'.join(svg))
 cairosvg.svg2png(url=str(svgfile),write_to=str(ROOT/'annotated'/f'{name}.png'))
 index[name]={'crop':crop,'width':width,'height':height,'marks':positions,'source':'screenshots/'+name+'.png'}
ROOT.joinpath('source/figure-index.json').write_text(json.dumps(index,indent=2))
print('Rendered',len(index),'editable annotated figures; every arrow target checked visible.')

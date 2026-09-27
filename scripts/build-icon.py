"""Export the existing capacity-bars mark. Pillow is needed only to rebuild assets."""
from pathlib import Path
from PIL import Image, ImageDraw

assets = Path(__file__).resolve().parents[1] / 'assets'
image = Image.new('RGBA', (512, 512))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 504, 504), radius=112, fill='#0c1013', outline='#263239', width=12)
for left, top in ((96, 272), (224, 176), (352, 80)):
    draw.rounded_rectangle((left, top, left + 64, 416), radius=20, fill='#7ae4c6')
image.resize((256, 256), Image.Resampling.LANCZOS).save(assets / 'ccx-icon.png')
image.save(assets / 'ccx-icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])

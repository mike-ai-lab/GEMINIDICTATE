from PIL import Image, ImageDraw, ImageFont
fnt = ImageFont.truetype('MaterialIcons-Regular.ttf', 20)
img = Image.new('RGBA', (32,32), (0,0,0,0))
d = ImageDraw.Draw(img)
d.text((16,16), '\ue029', font=fnt, fill='black', anchor='mm')
img.save('mic_test.png')
print('ok')

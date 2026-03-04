"""Generate Realtime app icon -- blue/cyan gradient circle with waveform."""
from PIL import Image, ImageDraw
import math

size = 256
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

center = size // 2
radius = size // 2 - 4

# Blue/cyan radial gradient circle
for r in range(radius, 0, -1):
    t = r / radius
    red = int(20 + 54 * (1 - t))
    green = int(100 + 158 * (1 - t) ** 0.7)
    blue = int(200 + 55 * (1 - t))
    draw.ellipse(
        [center - r, center - r, center + r, center + r],
        fill=(red, green, blue, 255)
    )

# Inner highlight
for r in range(int(radius * 0.6), int(radius * 0.2), -1):
    t = (r - radius * 0.2) / (radius * 0.4)
    alpha = int(40 * (1 - t))
    draw.ellipse(
        [center - r - 15, center - r - 20, center + r - 15, center + r - 20],
        fill=(200, 255, 255, alpha)
    )

# Waveform across the center
wave_y = center
wave_amp = radius * 0.28
wave_freq = 3.5
line_width = 6

points = []
for x in range(int(center - radius * 0.7), int(center + radius * 0.7)):
    t = (x - (center - radius * 0.7)) / (radius * 1.4)
    envelope = abs(math.sin(t * math.pi)) ** 0.6
    y = wave_y + wave_amp * envelope * math.sin(t * wave_freq * 2 * math.pi)
    points.append((int(x), int(round(y))))

# Draw thick waveform
for i in range(len(points) - 1):
    draw.line([points[i], points[i + 1]], fill=(255, 255, 255, 230), width=line_width)

# Thinner bright core
for i in range(len(points) - 1):
    draw.line([points[i], points[i + 1]], fill=(220, 250, 255, 255), width=3)

# Save as ICO with multiple sizes
icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ico_path = r'C:\Users\Patrick\Realtime\assets\realtime.ico'
img.save(ico_path, format='ICO', sizes=icon_sizes)
print(f"Icon created: {ico_path}")

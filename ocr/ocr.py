import ddddocr

# 初始化OCR对象
ocr = ddddocr.DdddOcr()

# 读取图片
from pathlib import Path

image_path = Path(__file__).parent / "1.jpg"
with image_path.open("rb") as f:
    image = f.read()

# 设置识别范围为数字
ocr.set_ranges(0)  # 等同于 ocr.set_ranges("0123456789")

# 识别图片
result = ocr.classification(image)
print(result)  # 输出识别结果
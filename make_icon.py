from PIL import Image
import os


def make_icon():
    # 自动找图片：优先 png，没有就找 jpg
    src = None
    for name in ["my_icon.png", "my_icon.jpg", "my_icon.jpeg"]:
        if os.path.exists(name):
            src = name
            break

    if not src:
        print("❌ 找不到 my_icon.png / my_icon.jpg")
        print("   请确认图片放在 D:\\PythonProject\\ 目录下")
        return

    print(f"📷 读取图片：{src}")

    try:
        img = Image.open(src).convert("RGBA")
    except Exception as e:
        print(f"❌ 读不到图片：{e}")
        return

    # 强制裁成正方形（取短边居中裁剪）
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        print(f"⚠ 图片不是正方形，已居中裁剪为 {side}×{side}")

    # 缩放到 256×256
    img = img.resize((256, 256), Image.LANCZOS)

    # 保存为 ico
    img.save(
        'icon.ico',
        format='ICO',
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print("✅ 图标已生成：icon.ico")


if __name__ == '__main__':
    make_icon()
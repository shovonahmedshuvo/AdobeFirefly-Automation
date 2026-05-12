
import os
import logging
from adobe_firefly import AdobeFireflyEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("firefly-test")

def main():
    image_path = r"D:\Desktop\Master folder\Test 1\image.png"
    output_path = r"D:\Desktop\Master folder\Test 1\image_no_bg.png"
    
    if not os.path.exists(image_path):
        logger.error(f"Image not found at {image_path}")
        return

    logger.info(f"Loading image from {image_path}")
    with open(image_path, "rb") as f:
        img_data = f.read()

    engine = AdobeFireflyEngine()
    
    logger.info("Starting background removal...")
    try:
        result = engine.process(img_data, "image.png")
        if result:
            with open(output_path, "wb") as f:
                f.write(result)
            logger.info(f"Success! Result saved to {output_path}")
        else:
            logger.error("Engine returned no result.")
    except Exception as e:
        logger.error(f"Automation failed: {e}")

if __name__ == "__main__":
    main()

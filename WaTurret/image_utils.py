import cv2
import numpy as np

def apply_hdr_filter(img):
    """
    Applies real-time Photoshop-style Shadow/Highlight HDR adjustments:
    - Converts to LAB space.
    - Uses a large Gaussian-blurred L channel as a smooth local illumination guide to prevent edge halos.
    - Selectively boosts deep shadows (L < 128) by up to +30 lightness points.
    - Selectively compresses bright highlights (L > 128) by up to -20 lightness points.
    - Leaves midtones balanced, preventing global overexposure.
    """
    try:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b_chan = cv2.split(lab)
        
        l_float = l.astype(np.float32)
        
        # 1. Create a smooth local illumination map
        guide = cv2.GaussianBlur(l, (33, 33), 0).astype(np.float32)
        
        # 2. Shadow boost: rises as guide falls below 128 (maximum +30 at 0 illumination)
        shadow_mask = np.clip((128.0 - guide) / 128.0, 0.0, 1.0)
        shadow_boost = shadow_mask * 30.0
        
        # 3. Highlight reduction: rises as guide increases above 128 (maximum -20 at 255 illumination)
        highlight_mask = np.clip((guide - 128.0) / 127.0, 0.0, 1.0)
        highlight_reduction = highlight_mask * 20.0
        
        # Apply local tone corrections
        l_hdr = l_float + shadow_boost - highlight_reduction
        l_hdr = np.clip(l_hdr, 0, 255).astype(np.uint8)
        
        hdr_lab = cv2.merge((l_hdr, a, b_chan))
        return cv2.cvtColor(hdr_lab, cv2.COLOR_LAB2BGR)
    except Exception:
        return img

def improve_image_cv(img):
    """
    Applies high-speed, professional-grade image enhancement optimized for OV3660:
    1. Local Adaptive Contrast Enhancement (LAB-CLAHE) to reveal shadows and balance highlights locally.
    2. Unsharp Masking to improve edge definition smoothly without amplifying high-frequency CMOS sensor noise.
    3. Moderate color saturation boost (25%) in HSV space.
    """
    try:
        # Step 1: CLAHE in LAB space
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        
        # clipLimit=1.5 and tileGridSize=(8,8) keeps contrast natural and low-noise
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        cl = clahe.apply(l)
        
        # Step 2: Unsharp Masking on the CLAHE-enhanced L channel
        # L_enhanced = 1.4 * L - 0.4 * L_blurred
        blurred_l = cv2.GaussianBlur(cl, (9, 9), 0)
        cl_sharpened = cv2.addWeighted(cl, 1.4, blurred_l, -0.4, 0)
        
        # Remerge channels back to BGR
        enhanced_lab = cv2.merge((cl_sharpened, a, b))
        img_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
        
        # Step 3: Color boost in HSV space
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        
        # Saturate by 25% (scale factor 1.25)
        s_boosted = np.clip(s.astype(np.float32) * 1.25, 0, 255).astype(np.uint8)
        
        improved_hsv = cv2.merge((h, s_boosted, v))
        return cv2.cvtColor(improved_hsv, cv2.COLOR_HSV2BGR)
    except Exception:
        return img

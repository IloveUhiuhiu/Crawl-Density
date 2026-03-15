import pandas as pd
import undetected_chromedriver as uc

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.keys import Keys
from google import genai
import re
import time
import os
import random
from dotenv import load_dotenv
load_dotenv()

# =========================
# CONFIG
# =========================

INPUT_CSV = "ingredients_metadata.csv"
OUTPUT_CSV = "output_density.csv"

BASE_URL = "https://www.aqua-calc.com/calculate/food-volume-to-weight"

CHROME_VERSION = 142

STEP_SLEEP = 5


API_KEY = os.getenv("API_KEY", None)
MODEL_ID = "gemini-2.5-flash-lite"
client = genai.Client(api_key=API_KEY)
# =========================
# START BROWSER
# =========================

def start_browser():

    print("🌐 Starting Chrome...")

    options = uc.ChromeOptions()

    options.add_argument("--start-maximized")

    driver = uc.Chrome(
        options=options,
        version_main=CHROME_VERSION
    )

    print("✅ Chrome ready")

    return driver


# =========================
# SEARCH INGREDIENT
# =========================

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.keys import Keys

def search_ingredient(driver, ingredient):
    MAX_RETRIES = 3 # Giảm retry vì ta sẽ xử lý tại chỗ
    wait = WebDriverWait(driver, 20)
    
    for attempt in range(MAX_RETRIES):
        print(f"\n🔎 Searching: {ingredient} (Attempt {attempt + 1}/{MAX_RETRIES})")
        
        try:
            # 1. Tìm ô search (Không dùng driver.get ở đây nữa)
            search_box = wait.until(EC.element_to_be_clickable((By.ID, "search-for-field")))
            
            # Xóa ô search bằng cách click và dùng phím tắt để tránh bị script web chặn clear()
            search_box.click()
            search_box.send_keys(Keys.CONTROL + "a")
            search_box.send_keys(Keys.DELETE)
            
            # 2. CƠ CHẾ NHẬP LIỆU NHƯ NGƯỜI: Gõ từng ký tự với delay ngẫu nhiên
            for char in ingredient:
                search_box.send_keys(char)
                time.sleep(random.uniform(0.1, 0.3)) # Delay giữa mỗi phím
            
            time.sleep(0.5)
            search_box.send_keys(Keys.ENTER)
            time.sleep(1)
            driver.execute_script("document.getElementById('density').click();")
            try:
                # Đợi cho đến khi dropdown có ít nhất 2 option (bao gồm option mặc định)
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#density option")) > 1)
                
                # Thay vì dùng Select(dropdown).options, hãy lấy trực tiếp text/value qua thực thi JS 
                # để tránh StaleElementReferenceException
                options_data = driver.execute_script("""
                    var sel = document.getElementById('density');
                    return Array.from(sel.options).map(opt => opt.text);
                """)
                
                candidates = [txt.strip() for txt in options_data if txt.strip() and "specify" not in txt.lower()]
                return candidates
            except Exception as e:
                print(f"⚠️ Lỗi khi lấy candidates: {e}")
                return []
        except (StaleElementReferenceException, TimeoutException):
            print(f"🔄 Đợi lâu quá hoặc trang refresh, đang thử lại...")
            # Nếu nghi ngờ kẹt, mới load lại trang làm phương án cuối
            if attempt == MAX_RETRIES - 1:
                driver.get(BASE_URL)
            time.sleep(2)
            
    return []
# =========================
# SELECT FIRST OPTION
# =========================

def select_candidate(driver, name):

    wait = WebDriverWait(driver, 10)

    dropdown = wait.until(
        EC.presence_of_element_located((By.ID, "density"))
    )

    select = Select(dropdown)

    print("👉 Selecting:", name)

    select.select_by_visible_text(name)

    time.sleep(STEP_SLEEP)

    wait.until(
        EC.presence_of_element_located((By.ID, "result1"))
    )


# =========================
# SCRAPE DENSITY
# =========================

def scrape_density(driver):
    try:
        wait = WebDriverWait(driver, 10)
        
        # Tìm thẻ <ul> có class="math"
        # "ul.math" nghĩa là: Tìm thẻ <ul> VÀ có class là "math"
        element = wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "ul.math"))
        )
        
        # Lấy toàn bộ văn bản bên trong
        raw_text = element.text.strip()
        print(f"DEBUG TEXT (from ul.math): \n{raw_text}")

        # --- LOGIC QUÉT DỮ LIỆU ---
        
        # Ưu tiên Metric Cup (250ml) để tính Density chuẩn nhất
        metric_pattern = r"1\s+metric\s+cup.*?weighs\s+([0-9.]+)\s+grams"
        match = re.search(metric_pattern, raw_text, re.I | re.S)

        if match:
            weight = float(match.group(1))
            density = round(weight / 250.0, 4)
            print(f"✅ Thành công! Weight: {weight}g -> Density: {density}")
            return density
        
        # Nếu không có Metric, thử tìm US Cup (236.59ml)
        us_pattern = r"1\s+US\s+cup.*?weighs\s+([0-9.]+)\s+grams"
        match_us = re.search(us_pattern, raw_text, re.I | re.S)
        if match_us:
            weight = float(match_us.group(1))
            density = round(weight / 236.59, 4)
            return density

    except Exception as e:
        print(f"❌ Không tìm thấy thẻ ul.math hoặc lỗi: {e}")
    
    return None



# =========================
# SAVE RESULT
# =========================

def save_result(row):

    df = pd.DataFrame([row])

    if not os.path.exists(OUTPUT_CSV):

        df.to_csv(OUTPUT_CSV, index=False)

    else:

        df.to_csv(
            OUTPUT_CSV,
            mode="a",
            header=False,
            index=False
        )

def gemini_select_best_candidate(ingredient, candidates):
    """
    Sử dụng Gemini để chọn item phù hợp nhất từ danh sách candidates.
    """
    if not candidates:
        return None
    
    if len(candidates) == 1:
        return candidates[0]

    # Tạo prompt để ép Gemini trả về kết quả chính xác
    prompt = f"""
    Original Ingredient: "{ingredient}"
    
    Candidates from search results:
    {chr(10).join([f"- {c}" for c in candidates])}

    Task:
    1. Select the BEST match from the candidate list.
    2. Selection Logic (Strict Priority):
       - If it's a FRUIT: Select the "raw", "fresh", or "whole" version.
       - If it's MEAT, VEGETABLE, or GRAIN: Select the "cooked", "roasted", "boiled", or "baked" version.
       - If none of the above specific states exist, select the most "generic/standard" version of the food.
    3. Mandatory Filters: 
       - NEVER select "babyfood", "strained", "juice", "liquid", or "dehydrated" versions.
       - Avoid specific commercial brands.
    4. Constraint 1: Your response must be EXACTLY the string from the candidate list.
    5. Constraint 2: If no candidate matches the ingredient at all, respond with "NONE".
    6. Constraint 3: Provide ONLY the string. No conversational text.

    Best Match:"""
    try:
        response = client.models.generate_content( model=MODEL_ID, contents=prompt ) 
        best_match = response.text.strip() 
        print("🤖 AI trả:", best_match)
        
        if "NONE" in best_match.upper():
            print(f"⏩ Skipping: No good match found for '{ingredient}'")
            return None

        # Kiểm tra xem Gemini có trả về đúng text trong list không (tránh hallucination)
        if best_match in candidates:
            print(f"🤖 Gemini chose: {best_match}")
            return best_match
        else:
            # Nếu Gemini trả về text hơi khác, tìm cái gần nhất hoặc fallback về cái đầu tiên
            print(f"⚠️ Gemini suggestion '{best_match}' not in list. Falling back to first candidate.")
            return candidates[0]
    except Exception as e:
        print(f"❌ Gemini Error: {e}")
        return candidates[0]

def get_processed_ingredients():
    """
    Kiểm tra file output_density.csv nếu tồn tại
    và trả về tập hợp những ingredient đã được xử lý
    """
    if os.path.exists(OUTPUT_CSV):
        try:
            df = pd.read_csv(OUTPUT_CSV)
            # Lấy toàn bộ ingredient đã xử lý (bất kể có density hay không)
            processed = set(df['ingredient'].tolist())
            precessed.extend(['cheese pizza', 'guacamole', 'artic char'])
            print(f"📋 Đã tìm thấy {len(processed)} ingredient trong file output")
            return processed
        except Exception as e:
            print(f"⚠️ Lỗi khi đọc file output: {e}")
            return set()
    return set()

# =========================
# PIPELINE
# =========================

def run_pipeline():
    df = pd.read_csv(INPUT_CSV)
    ingredients = df["ingr"].dropna().tolist()
    driver = start_browser()
    driver.get(BASE_URL)
    processed = get_processed_ingredients()
    remaining_ingredients = [i for i in ingredients if i not in processed]

    try:
        for ingredient in remaining_ingredients:
            print("\n" + "="*30)
            print(f"INGREDIENT: {ingredient}")
            print("="*30)

            candidates = search_ingredient(driver, ingredient)

            if not candidates:
                print("❌ No candidates")
                continue

            # THAY ĐỔI Ở ĐÂY: Thay vì lấy candidates[0], gọi Gemini
            best = gemini_select_best_candidate(ingredient, candidates)

            select_candidate(driver, best)

            density = scrape_density(driver)

            row = {
                "ingredient": ingredient,
                "matched_name": best,
                "density": density
            }

            save_result(row)
            time.sleep(STEP_SLEEP)

    finally:
        driver.quit()

# =========================
# MAIN
# =========================

if __name__ == "__main__":

    run_pipeline()
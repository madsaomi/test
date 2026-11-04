import os
import io
from PIL import Image, ImageDraw, ImageFont

from text_utils import normalize_text

WIDTH = 2048
HEIGHT = 1012

# Фирменные цвета (brandbook 2021): желтый #FFB703, черный #070707
BRAND_YELLOW = (255, 183, 3)
BRAND_BLACK = (7, 7, 7)
BRAND_TINT = (255, 243, 216)   # Светлый оттенок фирменного желтого для подложек

def get_font(size=14, bold=False, serif=False):
    """Возвращает системный шрифт с поддержкой кириллицы с fallback.
    serif=True — фирменный шрифт заголовков (аналог Minion Pro)."""
    if serif:
        font_names = ['georgiab.ttf', 'timesbd.ttf'] if bold else ['georgia.ttf', 'times.ttf']
        linux_fonts = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf'
        ]
    else:
        font_names = ['arialbd.ttf', 'segoeuib.ttf'] if bold else ['arial.ttf', 'segoeui.ttf']
        linux_fonts = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'
        ]
    windir = os.environ.get('WINDIR', 'C:\\Windows')
    for fn in font_names:
        p = os.path.join(windir, 'Fonts', fn)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass

    # Fallback для Linux / Docker
    for lf in linux_fonts:
        if os.path.exists(lf):
            try:
                return ImageFont.truetype(lf, size)
            except Exception:
                pass

    return ImageFont.load_default()

def draw_dotted_line(draw, x1, y1, x2, dot_spacing=8, dot_radius=1.5, color=(120, 120, 120)):
    x = x1
    while x <= x2:
        draw.ellipse((x - dot_radius, y1 - dot_radius, x + dot_radius, y1 + dot_radius), fill=color)
        x += dot_spacing

def draw_vector_checkmark(draw, x, y, size=18, color=(22, 163, 74, 255), width=3):
    p1 = (x, y + size * 0.5)
    p2 = (x + size * 0.35, y + size * 0.9)
    p3 = (x + size * 0.95, y + size * 0.05)
    draw.line([p1, p2, p3], fill=color, width=width)

def draw_vector_cross(draw, x, y, size=16, color=(220, 38, 38, 255), width=3):
    p1 = (x, y)
    p2 = (x + size, y + size)
    p3 = (x + size, y)
    p4 = (x, y + size)
    draw.line([p1, p2], fill=color, width=width)
    draw.line([p3, p4], fill=color, width=width)

def generate_blank_canvas():
    """
    Создает кристально чистую основу официального бланка
    в сверхвысоком разрешении (2048 x 1012 px).
    """
    img = Image.new('RGB', (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    COLOR_BLACK = BRAND_BLACK

    font_title = get_font(34, bold=True, serif=True)  # Фирменный серифный заголовок
    font_sub = get_font(18, bold=False)
    font_field_label = get_font(20, bold=True)
    font_num = get_font(19, bold=True)
    font_circle = get_font(17, bold=False)
    font_box_num = get_font(26, bold=True)

    # 1. Внешняя рамка бланка
    draw.rounded_rectangle((20, 20, WIDTH - 20, HEIGHT - 20), radius=32, outline=COLOR_BLACK, width=4)

    # 2. Шапка
    # Фирменный логотип Stanford language center (черный lockup)
    logo_path = 'static/logo_lockup_black.png'
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            target_h = 104
            scale = target_h / logo.height
            logo_scaled = logo.resize((round(logo.width * scale), target_h), Image.Resampling.LANCZOS)
            img.paste(logo_scaled, (48, 22), logo_scaled)
        except Exception:
            draw.text((50, 50), "Stanford Language Center", fill=COLOR_BLACK, font=font_title)
    else:
        draw.text((50, 50), "Stanford Language Center", fill=COLOR_BLACK, font=font_title)

    draw.text((WIDTH - 45, 45), "Б Л А Н К   О Т В Е Т О В", fill=COLOR_BLACK, font=font_title, anchor="ra")
    draw.text((WIDTH - 45, 95), "1–45: закрасьте или обведите букву   |   46–50: форма глагола", fill=COLOR_BLACK, font=font_sub, anchor="ra")

    # Фирменная желтая разделительная полоса
    draw.rounded_rectangle((40, 132, WIDTH - 40, 146), radius=7, fill=BRAND_YELLOW)

    # 3. Блок данных студента
    draw.text((45, 178), "№", fill=COLOR_BLACK, font=font_field_label)
    draw.rounded_rectangle((85, 165, 235, 218), radius=16, outline=COLOR_BLACK, width=3)

    draw.text((275, 178), "ФИО", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 345, 208, 1100)

    draw.text((1150, 178), "Учеба / Работа", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 1340, 208, WIDTH - 45)

    draw.text((45, 248), "Номер тел. 1", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 200, 276, 1000)

    draw.text((1050, 248), "Номер тел. 2", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 1205, 276, WIDTH - 45)

    # 4. Основная сетка ответов 1 - 45
    grid_left = 40
    grid_top = 312
    grid_right = WIDTH - 40
    grid_bottom = 728
    grid_width = grid_right - grid_left
    grid_height = grid_bottom - grid_top

    col_width = grid_width / 5.0
    num_col_w = 48
    row_height = grid_height / 9.0

    # Светло-желтые подложки под колонки с номерами вопросов (фирменный акцент)
    for c in range(5):
        c_left = grid_left + c * col_width
        draw.rectangle((c_left + 2, grid_top + 2, c_left + num_col_w, grid_bottom - 2), fill=BRAND_TINT)

    draw.rectangle((grid_left, grid_top, grid_right, grid_bottom), outline=COLOR_BLACK, width=4)

    circle_letters = ['a', 'b', 'c', 'd']
    circle_radius = 16

    for c in range(5):
        c_left = grid_left + c * col_width
        c_right = c_left + col_width

        if c > 0:
            draw.line((c_left, grid_top, c_left, grid_bottom), fill=COLOR_BLACK, width=3)

        draw.line((c_left + num_col_w, grid_top, c_left + num_col_w, grid_bottom), fill=COLOR_BLACK, width=2)

        opt_area_start = c_left + num_col_w
        opt_area_width = c_right - opt_area_start
        opt_step = opt_area_width / 4.0

        for r in range(9):
            q_num = c * 9 + r + 1
            r_center_y = grid_top + (r + 0.5) * row_height

            draw.text((c_left + num_col_w / 2, r_center_y), str(q_num), fill=COLOR_BLACK, font=font_num, anchor="mm")

            for oi, l in enumerate(circle_letters):
                opt_cx = opt_area_start + (oi + 0.5) * opt_step
                draw.ellipse(
                    (opt_cx - circle_radius, r_center_y - circle_radius, opt_cx + circle_radius, r_center_y + circle_radius),
                    outline=COLOR_BLACK, width=2
                )
                draw.text((opt_cx, r_center_y - 1), l, fill=COLOR_BLACK, font=font_circle, anchor="mm")

    # 5. Блоки вопросов 46 - 50 (Форма глагола)
    box_row1_y = 752
    box_row2_y = 836
    box_h = 68
    box_w = (WIDTH - 80 - 40) / 3.0

    def draw_open_box(x, y, w, h, num_str):
        num_block_w = 72
        # Фирменный желтый блок с черным номером
        draw.rectangle((x, y, x + num_block_w, y + h), fill=BRAND_YELLOW)
        draw.text((x + num_block_w / 2, y + h / 2), num_str, fill=BRAND_BLACK, font=font_box_num, anchor="mm")
        draw.rectangle((x + num_block_w, y, x + w, y + h), outline=COLOR_BLACK, width=3)

    draw_open_box(40, box_row1_y, box_w, box_h, "46")
    draw_open_box(40 + box_w + 20, box_row1_y, box_w, box_h, "47")
    draw_open_box(40 + (box_w + 20) * 2, box_row1_y, box_w, box_h, "48")

    draw_open_box(40, box_row2_y, box_w, box_h, "49")
    draw_open_box(40 + box_w + 20, box_row2_y, box_w, box_h, "50")

    # 6. Футер
    draw.text((45, 940), "Откуда узнали про Stanford LC?", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 420, 966, 1200)

    draw.text((1260, 940), "Дата заполнения", fill=COLOR_BLACK, font=font_field_label)
    draw_dotted_line(draw, 1470, 966, WIDTH - 45)

    return img

def generate_filled_sheet(*args, **kwargs):
    """
    Генерирует официальный бланк в высоком разрешении (2048 x 1012 px)
    с программной отрисовкой и точным наложением ответов студента.
    Поддерживает как передачу (template_path, result_data, all_questions), так и (result_data=...).
    """
    result_data = kwargs.get('result_data')
    all_questions = kwargs.get('all_questions')
    
    for arg in args:
        if isinstance(arg, dict) and result_data is None:
            result_data = arg
        elif isinstance(arg, list) and all_questions is None:
            all_questions = arg
    img = generate_blank_canvas()
    
    if not result_data:
        # Возвращаем чистый бланк
        out_io = io.BytesIO()
        img.save(out_io, format='JPEG', quality=95)
        out_io.seek(0)
        return out_io

    overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    font_val = get_font(24, bold=True)
    font_small = get_font(18, bold=False)
    font_circle_ans = get_font(17, bold=True)
    font_id = get_font(25, bold=True)

    COLOR_CORRECT = (22, 163, 74, 255)       # Зеленый
    COLOR_WRONG = (220, 38, 38, 255)         # Красный
    COLOR_CORRECT_RING = (34, 197, 94, 255)  # Зеленый контур для подсказки
    COLOR_TEXT = (20, 20, 20, 255)
    COLOR_GRAY = (110, 110, 110, 255)

    # 1. Заполняем шапку студента
    res_id = str(result_data.get('id', '1'))
    draw.text((160, 191), f"#{res_id}", fill=COLOR_TEXT, font=font_id, anchor="mm")

    fio = str(result_data.get('fio', ''))[:40]
    draw.text((360, 175), fio, fill=COLOR_TEXT, font=font_val)

    work_study = str(result_data.get('work_study', '')).strip()[:35]
    if work_study:
        draw.text((1350, 175), work_study, fill=COLOR_TEXT, font=font_val)

    phone = str(result_data.get('phone', ''))
    draw.text((215, 245), phone, fill=COLOR_TEXT, font=font_val)

    phone2 = str(result_data.get('phone2', '')).strip()
    if phone2:
        draw.text((1220, 245), phone2, fill=COLOR_TEXT, font=font_val)

    source = str(result_data.get('source', '')).strip()[:35]
    if source:
        draw.text((435, 936), source, fill=COLOR_TEXT, font=font_val)

    date_str = str(result_data.get('date', ''))
    draw.text((1490, 936), date_str, fill=COLOR_TEXT, font=font_val)

    # Экзаменационный штамп / бейдж проверки по центру шапки
    score = result_data.get('score', 0)
    total = result_data.get('total', 50)
    percentage = round((score / total) * 100, 1) if total > 0 else 0
    level = str(result_data.get('level', ''))
    branch = str(result_data.get('branch', ''))

    badge_box = (540, 30, 1280, 126)
    # Светлая брендовая подложка
    draw.rounded_rectangle(badge_box, radius=14, fill=(255, 252, 240, 245), outline=BRAND_YELLOW, width=3)
    
    font_badge_score = get_font(23, bold=True)
    font_badge_sub = get_font(18, bold=True)
    
    score_line = f"РЕЗУЛЬТАТ: {score} из {total}  ({percentage}%)"
    level_line = f"УРОВЕНЬ: {level.upper()}  •  ФИЛИАЛ: {branch.upper()}"
    draw.text((910, 56), score_line, fill=COLOR_TEXT, font=font_badge_score, anchor="mm")
    draw.text((910, 98), level_line, fill=(22, 101, 52, 255), font=font_badge_sub, anchor="mm")

    # 2. Вопросы 1 - 45 с математически идеальным попаданием
    grid_left = 40
    grid_top = 312
    grid_width = (WIDTH - 40) - grid_left
    grid_height = 728 - grid_top

    col_width = grid_width / 5.0
    num_col_w = 48
    row_height = grid_height / 9.0
    opt_area_width = col_width - num_col_w
    opt_step = opt_area_width / 4.0

    circle_letters = ['a', 'b', 'c', 'd']
    circle_radius = 16.0

    user_answers = result_data.get('answers', {})

    for q in (all_questions or []):
        q_id = q.get('id')
        if not q_id or q_id > 45:
            continue

        idx = q_id - 1
        col_idx = idx // 9
        row_idx = idx % 9

        c_left = grid_left + col_idx * col_width
        opt_area_start = c_left + num_col_w
        r_center_y = grid_top + (row_idx + 0.5) * row_height

        options = q.get('options', [])
        correct_raw = q.get('answer', '')
        user_raw = user_answers.get(str(q_id), '')

        correct_idx = None
        for oi, opt in enumerate(options):
            if normalize_text(opt) == normalize_text(correct_raw):
                correct_idx = oi
                break

        user_idx = None
        for oi, opt in enumerate(options):
            if normalize_text(opt) == normalize_text(user_raw):
                user_idx = oi
                break

        if user_idx is not None:
            is_correct = (user_idx == correct_idx)
            ux = opt_area_start + (user_idx + 0.5) * opt_step
            u_color = COLOR_CORRECT if is_correct else COLOR_WRONG

            # Закрашиваем кружок с идеальным центрированием
            draw.ellipse((ux - circle_radius, r_center_y - circle_radius, ux + circle_radius, r_center_y + circle_radius), fill=u_color)
            draw.text((ux, r_center_y - 1), circle_letters[user_idx], fill=(255, 255, 255), font=font_circle_ans, anchor="mm")

            # Если ошибка - обводим правильный кружок зеленым кольцом
            if not is_correct and correct_idx is not None:
                cx_corr = opt_area_start + (correct_idx + 0.5) * opt_step
                draw.ellipse(
                    (cx_corr - circle_radius - 1, r_center_y - circle_radius - 1, cx_corr + circle_radius + 1, r_center_y + circle_radius + 1),
                    outline=COLOR_CORRECT_RING, width=3
                )
        else:
            # Пропущенный вопрос
            if correct_idx is not None:
                cx_corr = opt_area_start + (correct_idx + 0.5) * opt_step
                draw.ellipse(
                    (cx_corr - circle_radius - 1, r_center_y - circle_radius - 1, cx_corr + circle_radius + 1, r_center_y + circle_radius + 1),
                    outline=(234, 179, 8, 220), width=3
                )

    # 3. Вопросы 46 - 50 (Форма глагола)
    box_row1_y = 752
    box_row2_y = 836
    box_h = 68
    box_w = (WIDTH - 80 - 40) / 3.0
    num_block_w = 72

    open_boxes = {
        46: (40 + num_block_w + 12, box_row1_y + 16, 40 + box_w - 10),
        47: (40 + box_w + 20 + num_block_w + 12, box_row1_y + 16, 40 + box_w*2 + 20 - 10),
        48: (40 + (box_w + 20)*2 + num_block_w + 12, box_row1_y + 16, WIDTH - 40 - 10),
        49: (40 + num_block_w + 12, box_row2_y + 16, 40 + box_w - 10),
        50: (40 + box_w + 20 + num_block_w + 12, box_row2_y + 16, 40 + box_w*2 + 20 - 10),
    }

    for q in (all_questions or []):
        q_id = q.get('id')
        if q_id not in open_boxes:
            continue

        bx_start, by, bx_end = open_boxes[q_id]
        raw_ans = q.get('answer', '')
        user_val = str(user_answers.get(str(q_id), '')).strip()

        norm_user = normalize_text(user_val)
        is_correct = False
        if isinstance(raw_ans, list):
            is_correct = any(norm_user == normalize_text(a) for a in raw_ans)
            correct_display = raw_ans[0]
            if len(raw_ans) > 1 and len(raw_ans[0]) < 12 and len(raw_ans[1]) < 12:
                correct_display = f"{raw_ans[0]} / {raw_ans[1]}"
        else:
            is_correct = (norm_user == normalize_text(raw_ans))
            correct_display = str(raw_ans)

        if not user_val:
            draw.text((bx_start, by + 4), f"—  [{correct_display}]", fill=COLOR_GRAY, font=font_small)
        elif is_correct:
            draw.text((bx_start, by), user_val, fill=COLOR_CORRECT, font=font_val)
            val_w = int(draw.textlength(user_val, font=font_val))
            draw_vector_checkmark(draw, bx_start + val_w + 12, by + 5, size=18, color=COLOR_CORRECT, width=3)
        else:
            draw.text((bx_start, by), user_val, fill=COLOR_WRONG, font=font_val)
            val_w = int(draw.textlength(user_val, font=font_val))
            draw_vector_cross(draw, bx_start + val_w + 10, by + 6, size=16, color=COLOR_WRONG, width=3)

            hint_text = f"[{correct_display}]"
            if len(hint_text) > 24:
                hint_text = f"[{correct_display[:22]}..]"
            draw.text((bx_start + val_w + 35, by + 4), hint_text, fill=COLOR_CORRECT, font=font_small)

    final_img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')

    out_io = io.BytesIO()
    final_img.save(out_io, format='JPEG', quality=95)
    out_io.seek(0)
    return out_io

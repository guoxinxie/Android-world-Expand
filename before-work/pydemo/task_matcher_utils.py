"""
共享任务匹配工具模块
用于 visualize_trace.py 和 analyze_consistency.py 统一的任务名匹配逻辑。
提供三种分组策略：按模板类型、按精确goal、按时间戳聚类。
"""
import os
import json
import re
import difflib
from datetime import datetime, timedelta
from functools import lru_cache



def find_metadata_file():
    """搜索 task_metadata.json"""
    possible_paths = [
        "task_metadata.json",
        "android_world/task_metadata.json",
        "android_world/task_evals/task_metadata.json",
        "../android_world/task_metadata.json",
        "../android_world/task_evals/task_metadata.json",
    ]
    for p in possible_paths:
        if os.path.exists(p):
            return p

    cwd = os.getcwd()
    for entry in os.listdir(cwd):
        full = os.path.join(cwd, entry)
        if os.path.isdir(full) and entry.startswith("outputs-"):
            candidate = os.path.join(full, "task_metadata.json")
            if os.path.exists(candidate):
                return candidate

    for root, dirs, files in os.walk(cwd):
        depth = root[len(cwd):].count(os.sep)
        if depth > 3:
            dirs.clear()
            continue
        if "task_metadata.json" in files:
            return os.path.join(root, "task_metadata.json")

    return None


def load_task_metadata():
    difficulty_map = {}
    metadata_path = find_metadata_file()
    if not metadata_path:
        return difficulty_map
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)
        for item in metadata:
            if "task_name" in item and "difficulty" in item:
                difficulty_map[item["task_name"]] = item["difficulty"]
    return difficulty_map


# ==================== 文本规范化 ====================

def normalize_template(template):
    t = template
    t = t.replace("arduia pro expense", "pro expense")
    t = t.replace("contains only", "contain only")
    t = t.replace("Answer with the category only. If there are multiples categories",
                  "Answer with the activity type only. If there are multiple types")
    t = t.replace("without using abbreviations.",
                  "where both the amount and unit exactly match the format in the recipe.")
    t = t.replace("Express your answer in meters as a single integer.",
                  "Express your answer as a single number in meters rounded to the nearest integer.")
    t = t.replace("Copy {file_name} in DCIM",
                  "In Simple Gallery Pro, copy {file_name} in DCIM")
    if "Save a track with waypoints" in t:
        t = "Save a track with waypoints {waypoints} in the OsmAnd maps app in the same order as listed."
    return t


def normalize_goal(goal_str):
    if not goal_str:
        return ""
    g = goal_str.strip()
    g = g.replace("Assume the week starts from Monday. ", "")
    g = g.replace("Assume the week starts from Monday.", "")
    g = re.sub(r", and rename it to \S+", "", g)
    g = re.sub(r" and change its name to \S+", "", g)
    return g


# ==================== 模板模式加载 ====================

def load_template_patterns():
    metadata_path = find_metadata_file()
    templates_map = []
    if not metadata_path:
        return templates_map
    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)
        for item in metadata:
            task_name = item.get("task_name")
            difficulty = item.get("difficulty")
            raw_template = item.get("task_template")
            if task_name and difficulty and raw_template:
                fixed_template = normalize_template(raw_template)
                pattern_str = re.escape(fixed_template)
                pattern_str = re.sub(r"\\\{[a-zA-Z0-9_]+\\\}", r".*?", pattern_str)
                pattern = re.compile(f"^{pattern_str}$", re.IGNORECASE | re.DOTALL)
                skeleton = re.sub(r"\{.*?\}", "", fixed_template)
                skeleton = re.sub(r"\s+", " ", skeleton).strip()
                templates_map.append({
                    "pattern": pattern,
                    "task_name": task_name,
                    "difficulty": difficulty,
                    "skeleton": skeleton,
                })
    return templates_map


# ==================== 扩展的硬编码匹配器 ====================

def hardcoded_task_matcher(goal_str):
    g = goal_str.replace("\n", " ")

    # --- 费用/Expense ---
    if "Add the following expenses into the pro expense" in g or "Add the following expenses into the arduia pro expense" in g:
        return "ExpenseAddMultiple" if (g.count("Expense:") > 1 or g.count("amount_dollars:") > 1) else "ExpenseAddSingle"
    if "Add the expenses from expenses" in g and "Simple Gallery" in g and "pro expense" in g:
        return "ExpenseAddMultipleFromGallery"
    if "Go through the transactions in my_expenses.txt in Markor" in g:
        return "ExpenseAddMultipleFromMarkor"
    if "Delete the following expenses from pro expense:" in g or "Delete the following expenses from arduia pro expense:" in g:
        return "ExpenseDeleteMultiple" if "," in g.split("pro expense:")[-1].split("expense:")[-1] else "ExpenseDeleteSingle"
    if "Delete all but one of any expenses in pro expense" in g or "Delete all but one of any expenses in arduia pro expense" in g:
        return "ExpenseDeleteDuplicates"

    # --- Markor ---
    if "Update the content of" in g and "in Markor and change its name to" in g:
        return "MarkorChangeNoteContent"
    if "Update the Markor note" in g and "and rename it to" in g:
        return "MarkorAddNoteHeader"
    if "Create a new folder in Markor named" in g:
        return "MarkorCreateFolder"
    if "Create a new note in Markor named" in g:
        return "MarkorCreateNote"
    if "Delete the note in Markor named" in g:
        return "MarkorDeleteNote"
    if "Merge the contents of Markor notes" in g:
        return "MarkorMergeNotes"
    if "Transcribe the contents of video" in g and "in Markor as a comma separated list" in g:
        return "MarkorTranscribeVideo"

    # --- Recipe ---
    if "Delete the following recipes from Broccoli app:" in g:
        return "RecipeDeleteMultipleRecipes"
    if "Add the following recipes into the Broccoli app:" in g:
        return "RecipeAddMultipleRecipes"
    if "Add the recipes from recipes" in g and "Markor" in g and "Broccoli" in g:
        return "RecipeAddMultipleRecipesFromMarkor"
    if "Add the recipes from recipes.jpg" in g and "Simple Gallery" in g and "Broccoli" in g:
        return "RecipeAddMultipleRecipesFromImage"
    if "Delete all but one of any recipes in the Broccoli app" in g:
        return "RecipeDeleteDuplicateRecipes"
    if "What quantity of" in g and "do I need for the recipe" in g:
        return "NotesRecipeIngredientCount"

    # --- OpenTracks ---
    if "What activities did I do" in g and "in the OpenTracks app" in g:
        return "SportsTrackerActivitiesOnDate"
    if "longest distance covered in a" in g and "in the OpenTracks app" in g:
        return "SportsTrackerLongestDistanceActivity"
    if "activities did I do this week in the OpenTracks app" in g:
        return "SportsTrackerActivitiesCountForWeek"
    if "What was the total duration of" in g and "in the OpenTracks app this week" in g:
        return "SportsTrackerTotalDurationForCategoryThisWeek"
    if "What was the total distance covered for" in g and "in the OpenTracks app from" in g:
        return "SportsTrackerTotalDistanceForCategoryOverInterval"

    # --- Simple Calendar Pro ---
    if "What events do I have in the next week in Simple Calendar Pro" in g:
        return "SimpleCalendarEventsInNextWeek"
    if "In Simple Calendar Pro, create a calendar event in two weeks from today" in g:
        return "SimpleCalendarAddOneEventInTwoWeeks"
    if "In Simple Calendar Pro, create a calendar event on" in g:
        return "SimpleCalendarAddOneEvent"
    if "Do I have any events" in g and "in Simple Calendar Pro" in g:
        return "SimpleCalendarAnyEventsOnDate"
    if "What is on my schedule for" in g and "in Simple Calendar Pro" in g:
        return "SimpleCalendarEventOnDateAtTime"
    if "What is my first event after" in g and "in Simple Calendar Pro" in g:
        return "SimpleCalendarFirstEventAfterStartTime"
    if "When is my next meeting with" in g and "in Simple Calendar Pro" in g:
        return "SimpleCalendarNextMeetingWithPerson"

    # --- Tasks ---
    if "How many tasks do I have due next week in Tasks app" in g:
        return "TasksDueNextWeek"
    if "What are my high priority tasks in Tasks app" in g:
        return "TasksHighPriorityTasks"

    # --- SMS ---
    if "Resend the message I just sent to" in g and "Simple SMS" in g:
        return "SimpleSmsResend"

    # --- Joplin ---
    if "How many attendees were present in the meeting titled" in g and "Joplin" in g:
        return "NotesMeetingAttendeeCount"
    if "How many to-dos do I have in the" in g and "Joplin" in g:
        return "NotesTodoItemCount"
    if "Is the note titled" in g and "in the Joplin app marked as a todo" in g:
        return "NotesIsTodo"

    # --- Retro Music ---
    if "Create a playlist in Retro Music titled" in g:
        return "RetroCreatePlaylist"

    # --- System ---
    if "Turn bluetooth off." in g or "Turn bluetooth on." in g:
        return "SystemBluetoothTurnOff"

    # --- Other ---
    if "copy" in g and "in DCIM and save a copy with the same name in Download" in g:
        return "SaveCopyOfReceiptTaskEval"
    if "Save a track with waypoints" in g and "in the OsmAnd maps app" in g:
        return "OsmAndTrack"

    return None


# ==================== 主匹配函数 ====================

_templates_cache = None
_difficulty_cache = None


def get_task_info_by_goal(goal_str, templates_map=None, difficulty_map=None):
    global _templates_cache, _difficulty_cache

    if not goal_str:
        return "unknown", "unknown"

    if templates_map is None:
        if _templates_cache is None:
            _templates_cache = load_template_patterns()
        templates_map = _templates_cache

    if difficulty_map is None:
        if _difficulty_cache is None:
            _difficulty_cache = load_task_metadata()
        difficulty_map = _difficulty_cache

    clean_goal = normalize_goal(goal_str)

    # Layer 1: template regex
    for t in templates_map:
        if t["pattern"].match(clean_goal):
            return t["difficulty"], t["task_name"]

    # Layer 2: hardcoded keywords
    hc_task_name = hardcoded_task_matcher(clean_goal)
    if hc_task_name:
        difficulty = difficulty_map.get(hc_task_name, "unknown")
        return difficulty, hc_task_name

    # Layer 3: difflib skeleton matching
    clean_skeleton = re.sub(r"\s+", " ", clean_goal).strip()
    best_match, best_ratio = None, 0.0
    for t in templates_map:
        ratio = difflib.SequenceMatcher(None, clean_skeleton, t["skeleton"]).ratio()
        if ratio > best_ratio:
            best_ratio, best_match = ratio, t
    if best_ratio > 0.60 and best_match:
        return best_match["difficulty"], best_match["task_name"]

    # Layer 4: fallback keyword grouping
    fallback_name = _generate_fallback_task_name(clean_goal)
    return "unknown", fallback_name


def _generate_fallback_task_name(goal_str):
    g = goal_str.strip()
    patterns = [
        (r"Delete the following recipes from Broccoli app:", "Broccoli_DeleteRecipes"),
        (r"Add the following recipes into the Broccoli app:", "Broccoli_AddRecipes"),
        (r"What was the total distance covered for (.+?) activities", r"SportsTracker_Distance_\1"),
        (r"What was the total duration of (.+?) activities", r"SportsTracker_Duration_\1"),
        (r"How many (.+?) activities did I do this week", r"SportsTracker_Count_\1"),
        (r"When is my next meeting with (.+?) in Simple Calendar", r"Calendar_NextMeeting_\1"),
        (r"Do I have any events (.+?) in Simple Calendar", r"Calendar_Events_\1"),
        (r"What is on my schedule for (.+?) in Simple Calendar", r"Calendar_Schedule_\1"),
        (r"What is my first event after (.+?) in Simple Calendar", r"Calendar_FirstEvent_\1"),
        (r"Is the note titled '(.+?)' in the Joplin", r"Joplin_IsTodo_\1"),
        (r"How many to-dos do I have in the '(.+?)' folder", r"Joplin_TodoCount_\1"),
        (r"How many attendees were present in the meeting titled '(.+?)'", r"Joplin_Attendees_\1"),
        (r"Resend the message I just sent to (.+?) in Simple SMS", r"SMS_Resend_\1"),
        (r"Create a playlist in Retro Music titled \"(.+?)\"", r"Retro_Playlist_\1"),
        (r"Create a new note in Markor named (.+?) with the following", r"Markor_CreateNote_\1"),
        (r"Add the expenses from expenses.jpg in Simple Gallery", "Expense_AddFromGallery"),
        (r"Delete all but one of any expenses in pro expense", "Expense_DeleteDuplicates"),
        (r"Turn bluetooth off", "System_BluetoothOff"),
    ]
    for pattern, replacement in patterns:
        m = re.search(pattern, g, re.IGNORECASE)
        if m:
            if "\\1" in replacement:
                captured = m.group(1).strip()
                captured = re.sub(r'[^\w]', '_', captured)[:30]
                return replacement.replace("\\1", captured)
            return replacement
    g_clean = re.sub(r'\s+', '_', g[:30])
    return f"Unknown_{g_clean}"


# ==================== 时间戳实验组分组 ====================

def extract_timestamp_from_dir(dir_name):
    """从目录名中提取 (prefix, timestamp_string, datetime_object)"""
    m = re.search(r'_(\d{8}_\d{6})$', dir_name)
    if not m:
        return dir_name, None, None
    ts_str = m.group(1)
    try:
        dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return dir_name, ts_str, None
    prefix = dir_name[:-(len(ts_str)+1)]
    return prefix, ts_str, dt


def cluster_traces_by_timestamp(traces, time_threshold_seconds=180):
   
    from collections import defaultdict
    groups_by_type = defaultdict(list)
    for t in traces:
        groups_by_type[t["task_name"]].append(t)

    all_clusters = []
    for task_name, group in groups_by_type.items():
        for t in group:
            _, ts_str, dt = extract_timestamp_from_dir(t["id"])
            t["_ts_str"] = ts_str
            t["_ts_dt"] = dt if dt else datetime.max

  
        group.sort(key=lambda t: t["_ts_dt"])


        clusters = []
        current = []
        for t in group:
            if not current:
                current.append(t)
            elif t["_ts_dt"] == datetime.max or current[-1]["_ts_dt"] == datetime.max:
                current.append(t)
            else:
                gap = (t["_ts_dt"] - current[-1]["_ts_dt"]).total_seconds()
                if gap <= time_threshold_seconds:
                    current.append(t)
                else:
                    clusters.append(current)
                    current = [t]
        if current:
            clusters.append(current)

        all_clusters.extend(clusters)

    for cluster in all_clusters:
        for t in cluster:
            t.pop("_ts_str", None)
            t.pop("_ts_dt", None)

    return all_clusters


def group_traces_exact_goal(traces, templates_map=None, difficulty_map=None):
   
    from collections import defaultdict
    groups = defaultdict(list)
    for trace in traces:
        goal = trace.get("goal", "")
        groups[goal].append(trace)
    return dict(groups)


def group_traces_experiment(traces, templates_map=None, difficulty_map=None):

    if templates_map is None:
        templates_map = load_template_patterns()
    if difficulty_map is None:
        difficulty_map = load_task_metadata()

    # 补充 task_name
    for t in traces:
        if "task_name" not in t:
            _, tn = get_task_info_by_goal(t.get("goal", ""), templates_map, difficulty_map)
            t["task_name"] = tn

    clusters = cluster_traces_by_timestamp(traces, time_threshold_seconds=180)

    result = []
    for i, cluster in enumerate(clusters):
        sample = cluster[0]
        goal = sample.get("goal", "")
        task_name = sample.get("task_name", "unknown")
        short_label = get_short_task_label(task_name)
        # 生成组标签：task_name + 序号
        if len(clusters) > 1:
            label = f"{task_name}_{i+1}"
        else:
            label = task_name
        result.append({
            "label": label,
            "task_name": task_name,
            "short_label": short_label,
            "goal": goal,
            "traces": cluster,
            "count": len(cluster),
        })
    return result


# ==================== 标签映射 ====================

def get_short_task_label(task_name):
    label_map = {
        "ExpenseAddMultipleFromGallery": "费用-从图库添加",
        "ExpenseDeleteDuplicates": "费用-删除重复",
        "ExpenseDeleteMultiple": "费用-删除多个",
        "ExpenseAddMultiple": "费用-添加多个",
        "RecipeDeleteMultipleRecipes": "食谱-删除多个",
        "RecipeAddMultipleRecipesFromMarkor": "食谱-从Markor添加",
        "RecipeAddMultipleRecipesFromImage": "食谱-从图库添加",
        "MarkorCreateFolder": "Markor-创建文件夹",
        "MarkorCreateNote": "Markor-创建笔记",
        "MarkorDeleteNote": "Markor-删除笔记",
        "MarkorMergeNotes": "Markor-合并笔记",
        "RetroCreatePlaylist": "Retro-创建播放列表",
        "SimpleCalendarAddOneEventInTwoWeeks": "日历-两周后添加事件",
        "SimpleCalendarAnyEventsOnDate": "日历-某日事件",
        "SimpleCalendarEventOnDateAtTime": "日历-某时事件",
        "SimpleCalendarFirstEventAfterStartTime": "日历-首个事件",
        "SimpleCalendarNextMeetingWithPerson": "日历-下次会议",
        "TasksDueNextWeek": "任务-下周到期",
        "TasksHighPriorityTasks": "任务-高优先级",
        "NotesMeetingAttendeeCount": "Joplin-会议人数",
        "NotesTodoItemCount": "Joplin-待办数量",
        "NotesIsTodo": "Joplin-是否待办",
        "SimpleSmsResend": "短信-重发",
        "SystemBluetoothTurnOff": "系统-关闭蓝牙",
        "SportsTrackerTotalDistanceForCategoryOverInterval": "运动-距离统计",
        "SportsTrackerTotalDurationForCategoryThisWeek": "运动-时长统计",
    }
    return label_map.get(task_name, task_name)

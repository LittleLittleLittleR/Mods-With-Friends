from fastapi import FastAPI, HTTPException
import asyncio
import httpx
from pydantic import BaseModel, model_validator, ConfigDict
from typing import Any

app = FastAPI()

NUSMODS_API = "https://api.nusmods.com/v2/2025-2026/modules/{moduleCode}.json"

# Maps NUSMods day strings to weekday numbers (Monday = 1)
DAY_MAP = {
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
}


# ---------- request / response models ----------

class UserConfig(BaseModel):
    modules: list[str] = []
    earliest_start: int | None = None        # minutes from midnight, e.g. 10*60 = 600
    latest_end: int | None = None            # minutes from midnight, e.g. 18*60 = 1080
    lunch_window: list[int] = [12 * 60, 14 * 60]  # [start_min, end_min]
    lunch_duration: int = 60                 # minutes
    days_without_lunch: list[int] = []       # weekday numbers (1=Mon … 5=Fri)
    days_without_class: list[int] = []       # weekday numbers with no classes allowed
    optional_classes: dict[str, list[str]] = {}        # module -> [lessonTypes]
    compulsory_classes: dict[str, dict[str, str]] = {} # module -> {lessonType -> classNo}
    enable_lunch_break: bool = True


class TimetableRequest(BaseModel):
    """
    Accepts a flat CONFIG-style body where each username is a top-level key:

    {
        "semester": 1,
        "users": ["A", "B"],
        "A": { <UserConfig fields> },
        "B": { <UserConfig fields> },
        "shared": {
            "CS2107": [["A", "B"]]
        }
    }
    """
    model_config = ConfigDict(extra="allow")

    semester: int = 1
    users: list[str]
    shared: dict[str, list[list[str]]] = {}
    user_configs: dict[str, UserConfig] = {}

    @model_validator(mode="before")
    @classmethod
    def parse_user_configs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        users = data.get("users", [])
        user_configs: dict[str, Any] = {}
        for username in users:
            if username in data:
                user_configs[username] = data[username]
        # Inject parsed user_configs so the field is populated
        result = dict(data)
        result["user_configs"] = user_configs
        return result

    def get_user_config(self, username: str) -> UserConfig:
        return self.user_configs.get(username, UserConfig())


class Lesson(BaseModel):
    classNo: str
    startTime: str
    endTime: str
    weeks: list[int]
    day: str
    lessonType: str
    venue: str


class ModuleTimetable(BaseModel):
    moduleCode: str
    title: str
    lessons: list[Lesson]


class UserTimetable(BaseModel):
    username: str
    timetable: list[ModuleTimetable]


# ---------- helpers ----------

async def fetch_module(client: httpx.AsyncClient, module_code: str) -> dict:
    url = NUSMODS_API.format(moduleCode=module_code)
    response = await client.get(url)
    if response.status_code != 200:
        raise HTTPException(
            status_code=404,
            detail=f"Module '{module_code}' not found on NUSMods."
        )
    return response.json()


def extract_lessons(module_data: dict, semester: int) -> list[dict]:
    for sem_data in module_data.get("semesterData", []):
        if sem_data["semester"] == semester:
            return sem_data.get("timetable", [])
    return []


def time_str_to_minutes(time_str: str) -> int:
    """Convert a HHMM string (e.g. '0830') to minutes from midnight."""
    return int(time_str[:2]) * 60 + int(time_str[2:])


def filter_lessons(
    lessons: list[dict],
    module_code: str,
    config: UserConfig,
) -> list[dict]:
    """
    Return only the lessons that satisfy the user's constraints:
    - days_without_class: skip lessons on these weekdays
    - earliest_start / latest_end: skip lessons outside the allowed time window
    - compulsory_classes: for pinned lesson types keep only the specified classNo
    """
    pinned = config.compulsory_classes.get(module_code, {})
    filtered = []

    for lesson in lessons:
        day_num = DAY_MAP.get(lesson.get("day", ""), 0)
        start_min = time_str_to_minutes(lesson.get("startTime", "0000"))
        end_min = time_str_to_minutes(lesson.get("endTime", "0000"))
        lesson_type = lesson.get("lessonType", "")
        class_no = lesson.get("classNo", "")

        # Skip days the user wants no classes
        if day_num in config.days_without_class:
            continue

        # Enforce earliest start time
        if config.earliest_start is not None and start_min < config.earliest_start:
            continue

        # Enforce latest end time
        if config.latest_end is not None and end_min > config.latest_end:
            continue

        # For pinned lesson types keep only the required classNo
        if lesson_type in pinned and class_no != pinned[lesson_type]:
            continue

        filtered.append(lesson)

    return filtered


def intersect_lessons(
    lessons_by_user: dict[str, list[dict]],
) -> list[dict]:
    """
    Return lessons that are valid for every user in the group.
    A lesson slot is identified by (lessonType, classNo).
    Only slots present in ALL users' filtered lesson lists are kept.
    """
    if not lessons_by_user:
        return []

    users = list(lessons_by_user.keys())
    if len(users) == 1:
        return lessons_by_user[users[0]]

    # Build sets of (lessonType, classNo) per user
    slot_sets = [
        {(l["lessonType"], l["classNo"]) for l in lessons_by_user[u]}
        for u in users
    ]
    common_slots = slot_sets[0].intersection(*slot_sets[1:])

    # Return the full lesson dicts for the common slots (using first user's data)
    return [l for l in lessons_by_user[users[0]] if (l["lessonType"], l["classNo"]) in common_slots]


# ---------- endpoints ----------

@app.get("/")
def read_root():
    return {"message": "Hello from Mods With Friends backend!"}


@app.post("/timetables", response_model=list[UserTimetable])
async def get_timetables(request: TimetableRequest):
    # Collect all unique module codes across all users:
    # both explicitly listed modules and any extra modules in compulsory_classes
    all_mod_codes: set[str] = set()
    for username in request.users:
        config = request.get_user_config(username)
        all_mod_codes.update(config.modules)
        all_mod_codes.update(config.compulsory_classes.keys())

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[fetch_module(client, code) for code in all_mod_codes],
            return_exceptions=True,
        )

    failed = [
        code
        for code, result in zip(all_mod_codes, results)
        if isinstance(result, Exception)
    ]
    if failed:
        raise HTTPException(
            status_code=404,
            detail=f"The following module(s) were not found on NUSMods: {', '.join(sorted(failed))}",
        )

    modules_by_code = {data["moduleCode"]: data for data in results}

    # Pre-compute filtered lessons per user per module
    filtered_lessons: dict[str, dict[str, list[dict]]] = {}
    for username in request.users:
        config = request.get_user_config(username)
        all_user_mods = list(config.modules) + [
            m for m in config.compulsory_classes if m not in config.modules
        ]
        filtered_lessons[username] = {}
        for mod_code in all_user_mods:
            module_data = modules_by_code.get(mod_code)
            if module_data is None:
                continue
            raw = extract_lessons(module_data, request.semester)
            filtered_lessons[username][mod_code] = filter_lessons(raw, mod_code, config)

    # Apply shared constraints: for each shared module group, keep only slots
    # that are valid for every member of the group
    for mod_code, groups in request.shared.items():
        for group in groups:
            # Only process users that are in this request
            active = [u for u in group if u in filtered_lessons and mod_code in filtered_lessons[u]]
            if len(active) < 2:
                continue
            common = intersect_lessons({u: filtered_lessons[u][mod_code] for u in active})
            for u in active:
                filtered_lessons[u][mod_code] = common

    # Build response
    user_timetables: list[UserTimetable] = []
    for username in request.users:
        config = request.get_user_config(username)
        all_user_mods = list(config.modules) + [
            m for m in config.compulsory_classes if m not in config.modules
        ]
        mod_timetables: list[ModuleTimetable] = []
        for mod_code in all_user_mods:
            module_data = modules_by_code.get(mod_code)
            if module_data is None:
                continue
            lessons = filtered_lessons[username].get(mod_code, [])
            mod_timetables.append(ModuleTimetable(
                moduleCode=mod_code,
                title=module_data.get("title", ""),
                lessons=[Lesson(**lesson) for lesson in lessons],
            ))
        user_timetables.append(UserTimetable(
            username=username,
            timetable=mod_timetables,
        ))

    return user_timetables

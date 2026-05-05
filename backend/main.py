from fastapi import FastAPI, HTTPException
import asyncio
import httpx
from pydantic import BaseModel, model_validator, ConfigDict
from typing import Any
from itertools import product

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

    # Build joint assignment of classes per module such that:
    # - For each module, exactly one class is chosen for every lessonType
    # - If multiple users take the same module, they get the same class choices
    # - Choices respect per-user filtered lessons and avoid time conflicts per user

    def lessons_overlap(a: dict, b: dict) -> bool:
        if a.get("day") != b.get("day"):
            return False
        a_start = time_str_to_minutes(a.get("startTime", "0000"))
        a_end = time_str_to_minutes(a.get("endTime", "0000"))
        b_start = time_str_to_minutes(b.get("startTime", "0000"))
        b_end = time_str_to_minutes(b.get("endTime", "0000"))
        return not (a_end <= b_start or a_start >= b_end)

    # Determine modules that actually need assignment (taken by at least one user)
    modules_to_assign: list[str] = []
    module_users: dict[str, list[str]] = {}
    for username in request.users:
        config = request.get_user_config(username)
        all_user_mods = list(config.modules) + [
            m for m in config.compulsory_classes if m not in config.modules
        ]
        for m in all_user_mods:
            module_users.setdefault(m, []).append(username)

    for m in module_users:
        modules_to_assign.append(m)

    # Precompute valid options per module: for each lessonType, list of lesson dicts
    module_options: dict[str, list[list[dict]]] = {}
    for mod in modules_to_assign:
        mod_data = modules_by_code.get(mod)
        if mod_data is None:
            continue
        raw_lessons = extract_lessons(mod_data, request.semester)
        lesson_types = sorted({l.get("lessonType") for l in raw_lessons})

        users_for_mod = module_users.get(mod, [])
        options_per_type: list[list[dict]] = []
        impossible = False
        for lt in lesson_types:
            classnos_sets: list[set[str]] = []
            lessons_by_class: dict[str, dict] = {}
            for u in users_for_mod:
                lessons = [l for l in filtered_lessons.get(u, {}).get(mod, []) if l.get("lessonType") == lt]
                classnos = {l.get("classNo") for l in lessons}
                classnos_sets.append(classnos)
                for l in lessons:
                    if l.get("classNo") not in lessons_by_class:
                        lessons_by_class[l.get("classNo")] = l

            if not classnos_sets:
                options_per_type.append([])
                continue

            common_classnos = set.intersection(*classnos_sets)
            if not common_classnos:
                impossible = True
                break

            opts = [lessons_by_class[c] for c in sorted(common_classnos)]
            options_per_type.append(opts)

        if impossible:
            raise HTTPException(status_code=400, detail=f"No common class options for module {mod} given user constraints")

        combos: list[list[dict]] = []
        for combo in product(*options_per_type):
            ok = True
            for i in range(len(combo)):
                for j in range(i + 1, len(combo)):
                    if lessons_overlap(combo[i], combo[j]):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                combos.append(list(combo))

        if not combos:
            raise HTTPException(status_code=400, detail=f"No non-overlapping class combinations for module {mod}")

        module_options[mod] = combos

    # Backtracking: assign one combination per module so that for each user, their assigned lessons do not clash
    assigned: dict[str, list[dict]] = {}
    user_selected: dict[str, list[dict]] = {u: [] for u in request.users}

    def backtrack(idx: int) -> bool:
        if idx >= len(modules_to_assign):
            return True
        mod = modules_to_assign[idx]
        combos = module_options.get(mod, [])
        users_for_mod = module_users.get(mod, [])
        for combo in combos:
            valid = True
            for u in users_for_mod:
                for lesson in combo:
                    if any(lessons_overlap(lesson, assigned_l) for assigned_l in user_selected[u]):
                        valid = False
                        break
                if not valid:
                    break
            if not valid:
                continue

            assigned[mod] = combo
            for u in users_for_mod:
                user_selected[u].extend(combo)

            if backtrack(idx + 1):
                return True

            assigned.pop(mod, None)
            for u in users_for_mod:
                for _ in range(len(combo)):
                    user_selected[u].pop()

        return False

    success = backtrack(0)
    if not success:
        raise HTTPException(status_code=400, detail="Unable to find non-conflicting timetables for given constraints")

    # Build per-user response from assigned modules
    user_timetables: list[UserTimetable] = []
    for username in request.users:
        config = request.get_user_config(username)
        all_user_mods = list(config.modules) + [
            m for m in config.compulsory_classes if m not in config.modules
        ]
        mod_timetables: list[ModuleTimetable] = []
        for mod in all_user_mods:
            module_data = modules_by_code.get(mod)
            if module_data is None:
                continue
            lessons = assigned.get(mod, []) if mod in assigned else []
            mod_timetables.append(ModuleTimetable(
                moduleCode=mod,
                title=module_data.get("title", ""),
                lessons=[Lesson(**lesson) for lesson in lessons],
            ))
        user_timetables.append(UserTimetable(username=username, timetable=mod_timetables))

    return user_timetables

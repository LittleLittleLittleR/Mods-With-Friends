from fastapi import FastAPI, HTTPException
import asyncio
import httpx
from pydantic import BaseModel

app = FastAPI()

NUSMODS_API = "https://api.nusmods.com/v2/2025-2026/modules/{moduleCode}.json"


# ---------- request / response models ----------

class UserMods(BaseModel):
    username: str
    mods: list[str]


class TimetableRequest(BaseModel):
    semester: int = 1
    users: list[UserMods]


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


# ---------- endpoints ----------

@app.get("/")
def read_root():
    return {"message": "Hello from Mods With Friends backend!"}


@app.post("/timetables", response_model=list[UserTimetable])
async def get_timetables(request: TimetableRequest):
    # collect all unique module codes across all users
    all_mod_codes = {mod for user in request.users for mod in user.mods}

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

    user_timetables: list[UserTimetable] = []
    for user in request.users:
        mod_timetables: list[ModuleTimetable] = []
        for mod_code in user.mods:
            module_data = modules_by_code[mod_code]
            lessons = extract_lessons(module_data, request.semester)
            mod_timetables.append(ModuleTimetable(
                moduleCode=mod_code,
                title=module_data.get("title", ""),
                lessons=[Lesson(**lesson) for lesson in lessons],
            ))
        user_timetables.append(UserTimetable(
            username=user.username,
            timetable=mod_timetables,
        ))

    return user_timetables

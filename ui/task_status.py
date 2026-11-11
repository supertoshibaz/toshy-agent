from __future__ import annotations

from typing import Any, Dict, Iterable, List


from core.task_review import build_task_status_prompt


def build_active_task_list_prompt(tasks: Iterable[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for task in tasks or []:
        if str(task.get("status") or "") != "in_progress":
            continue
        task_id = str(task.get("id") or "").strip()
        text = str(task.get("text") or "").replace("\u200b", "").strip()
        if task_id and text:
            rows.append(f"- [In progress] [{task_id}] {text}")
    task_block = "\n".join(rows) or "- [In progress] (no tasks)"
    return "Current in-progress tasks:\n\n" + task_block


def run_task_status_check(client, session: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(session.get("session_id") or "")
    branch = str(session.get("active_branch") or "main")
    tasks = [
        dict(task)
        for task in (session.get("tasks") or [])
        if str(task.get("status") or "") == "in_progress"
    ]
    expected_ids = [str(task.get("id") or "") for task in tasks if task.get("id")]
    if not session_id or not tasks:
        return {"session_id": session_id, "success": True, "skipped": True, "updated": []}

    before = client.get_tasks(session_id, branch)
    prompt = build_task_status_prompt(tasks)
    answer = client.run_chat_message(
        session_id,
        branch,
        prompt,
        additional_kwargs={
            "task_status_check": True,
            "task_status_display": "Проверь статусы задач",
            "task_status_refresh": True,
            "task_status_task_ids": expected_ids,
        },
    )
    state = client.get_tasks(session_id, branch)
    success = state.get("report_count", 0) > before.get("report_count", 0) or not any(t.get("status") == "in_progress" for t in state.get("tasks", []))
    return {
        "session_id": session_id,
        "branch": branch,
        "success": success,
        "updated": expected_ids if success else [],
        "answer": answer,
        "error": "Модель не передала валидный отчёт через report_task_statuses." if not success else "",
    }


def run_dirty_task_status_checks(client, sessions: Iterable[Dict[str, Any]], progress=None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for index, session in enumerate(sessions or []):
        if progress:
            progress(f'Проверка {index + 1}/{len(sessions)}: {session.get("session_id")} / {session.get("active_branch", "main")}')
        try:
            results.append(run_task_status_check(client, dict(session)))
        except Exception as error:
            results.append({
                "session_id": str((session or {}).get("session_id") or ""),
                "success": False,
                "branch": (session or {}).get("active_branch", "main"),
                "error": str(error),
            })
    return results

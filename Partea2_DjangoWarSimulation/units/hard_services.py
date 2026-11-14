from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import requests

from django.db import OperationalError, close_old_connections, transaction

from .models import Fight, FightEvent, FightTurn, Fighter, Person

API_HOST = "https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net"

RANK_CONFIG = {
    "private": (Fighter.UnitType.INFANTRY, 10),
    "sergeant": (Fighter.UnitType.CAVALRY, 20),
    "lieutenant": (Fighter.UnitType.KNIGHT, 30),
    "general": (Fighter.UnitType.CANNON, 45),
}

_DB_WRITE_LOCK = threading.Lock()
_REGISTRY_LOCK = threading.Lock()
_SOLDIER_LOCKS: dict[int, threading.Lock] = {}


def _get_soldier_lock(soldier_id: int) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _SOLDIER_LOCKS.get(soldier_id)
        if lock is None:
            lock = threading.Lock()
            _SOLDIER_LOCKS[soldier_id] = lock
        return lock


def _run_db_write(fn, retries: int = 8):
    """Execute a short atomic DB write protected against SQLite lock contention."""
    for attempt in range(retries):
        try:
            with _DB_WRITE_LOCK:
                with transaction.atomic():
                    return fn()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
            
def fetch_with_retry(url: str, max_retries: int = 12, timeout: float = 15.0) -> dict:
    """HTTP GET with fast retry policy and live terminal progress."""
    last_error = None
    for attempt in range(max_retries):
        try:
            print(f"[SYNC] Fetching {url} (attempt {attempt + 1}/{max_retries})...", flush=True)
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            print(f"[SYNC] Timeout/error on {url}: {exc} -> retrying...", flush=True)
            if attempt < max_retries - 1:
                time.sleep(min(2.0, 0.3 * (2**attempt)))
    raise RuntimeError(f"Failed fetching {url} after {max_retries} attempts: {last_error}")

def _rank_to_stats(rank: str) -> tuple[str, int]:
    return RANK_CONFIG.get((rank or "").strip().lower(), (Fighter.UnitType.INFANTRY, 15))


def _ensure_fighter(
    external_id: int,
    name: str = "",
    age: int | None = None,
    rank: str = "",
    team: str | None = None,
    health: int = 100,
) -> Fighter:
    unit_type, attack_points = _rank_to_stats(rank)
    resolved_team = team or (Fighter.Team.BLUE if external_id % 2 == 0 else Fighter.Team.RED)
    resolved_name = name or f"Soldier #{external_id}"
    avatar_url = f"https://api.dicebear.com/9.x/adventurer/svg?seed={external_id}"

    fighter, created = Fighter.objects.get_or_create(
        external_id=external_id,
        defaults={
            "name": resolved_name,
            "age": age,
            "rank": rank,
            "unit_type": unit_type,
            "team": resolved_team,
            "health": max(0, min(100, health)),
            "attack_points": attack_points,
            "profile_image_link": avatar_url,
        },
    )
    if not created:
        updated_fields = []
        if name and fighter.name != name:
            fighter.name = name
            updated_fields.append("name")
        if age is not None and fighter.age != age:
            fighter.age = age
            updated_fields.append("age")
        if rank and fighter.rank != rank:
            fighter.rank = rank
            fighter.unit_type = unit_type
            fighter.attack_points = attack_points
            updated_fields.extend(["rank", "unit_type", "attack_points"])
        if team and fighter.team != team:
            fighter.team = team
            updated_fields.append("team")
        if updated_fields:
            fighter.save(update_fields=updated_fields)
    return fighter


def sync_people() -> int:
    payload = fetch_with_retry(f"{API_HOST}/people")
    people_data = payload.get("data", [])

    def _write():
        count = 0
        for item in people_data:
            Person.objects.update_or_create(
                external_id=item["id"],
                defaults={
                    "name": item["name"],
                    "age": item["age"],
                },
            )
            count += 1
        return count

    return _run_db_write(_write)


def _process_draft_batch(batch_items: list[dict]) -> int:
    """Background worker task: assign rank to Person and draft them as a Fighter."""
    close_old_connections()
    try:
        def _write():
            drafted = 0
            for item in batch_items:
                ext_id = item["id"]
                rank = item.get("rank", "")
                health = item.get("health", 100)
                Person.objects.update_or_create(
                    external_id=ext_id,
                    defaults={
                        "name": item["name"],
                        "age": item["age"],
                        "rank": rank,
                        "is_drafted": True,
                    },
                )
                _ensure_fighter(
                    external_id=ext_id,
                    name=item["name"],
                    age=item["age"],
                    rank=rank,
                    health=health,
                )
                drafted += 1
            return drafted

        return _run_db_write(_write)
    finally:
        close_old_connections()


def sync_and_draft_batches(max_workers: int = 6) -> int:
    """Download /draft/<batch> sequentially and dispatch each batch immediately to a background worker."""
    batch_id = 0
    total_drafted = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        while batch_id is not None:
            payload = fetch_with_retry(f"{API_HOST}/draft/{batch_id}")
            batch_items = payload.get("data", [])
            futures.append(executor.submit(_process_draft_batch, batch_items))
            batch_id = payload.get("next_batch")

        for future in as_completed(futures):
            total_drafted += future.result()
    return total_drafted


def _ingest_single_fight(fight_payload: dict) -> int:
    """Background worker task: persist a fight and its event steps in the database."""
    close_old_connections()
    try:
        def _write():
            ext_fight_id = fight_payload["fight_id"]
            blue_id = fight_payload["blue_soldier_id"]
            red_id = fight_payload["red_soldier_id"]
            steps = fight_payload.get("steps", [])

            blue_person = Person.objects.filter(external_id=blue_id).first()
            red_person = Person.objects.filter(external_id=red_id).first()

            blue_fighter = _ensure_fighter(
                external_id=blue_id,
                name=blue_person.name if blue_person else "",
                age=blue_person.age if blue_person else None,
                rank=blue_person.rank if blue_person else "",
                team=Fighter.Team.BLUE,
            )
            red_fighter = _ensure_fighter(
                external_id=red_id,
                name=red_person.name if red_person else "",
                age=red_person.age if red_person else None,
                rank=red_person.rank if red_person else "",
                team=Fighter.Team.RED,
            )

            fighters_by_ext = {blue_id: blue_fighter, red_id: red_fighter}
            fight, created = Fight.objects.get_or_create(
                external_fight_id=ext_fight_id,
                defaults={
                    "first_fighter": blue_fighter,
                    "second_fighter": red_fighter,
                    "is_simulated": False,
                },
            )
            if not created:
                fight.first_fighter = blue_fighter
                fight.second_fighter = red_fighter
                fight.save(update_fields=["first_fighter", "second_fighter"])
                FightEvent.objects.filter(fight=fight).delete()

            events = []
            for idx, step in enumerate(steps, start=1):
                attacker = fighters_by_ext.get(step["attacker"]) or _ensure_fighter(step["attacker"])
                defender = fighters_by_ext.get(step["defender"]) or _ensure_fighter(step["defender"])
                events.append(
                    FightEvent(
                        fight=fight,
                        step_number=idx,
                        attacker=attacker,
                        defender=defender,
                        damage=step["damage"],
                    )
                )
            FightEvent.objects.bulk_create(events)
            return fight.pk

        return _run_db_write(_write)
    finally:
        close_old_connections()


def sync_fight_events(max_workers: int = 8) -> int:
    """Download all fight batches and persist each fight's events via parallel background workers."""
    batch_id = 0
    total_fights = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        while batch_id is not None:
            payload = fetch_with_retry(f"{API_HOST}/fight/{batch_id}")
            fights_data = payload.get("data", [])
            for fight_item in fights_data:
                futures.append(executor.submit(_ingest_single_fight, fight_item))
            batch_id = payload.get("next_batch")

        for future in as_completed(futures):
            future.result()
            total_fights += 1
    return total_fights


def _replay_single_fight(fight_id: int) -> int:
    """
    Replay one fight in a background thread.
    Acquires per-soldier locks in ascending fighter PK order to prevent deadlocks
    and lost-update race conditions when the same soldier fights concurrently.
    """
    close_old_connections()
    try:
        fight = Fight.objects.select_related("first_fighter", "second_fighter").get(pk=fight_id)
        events = list(FightEvent.objects.filter(fight=fight).order_by("step_number"))

        first_id = fight.first_fighter_id
        second_id = fight.second_fighter_id
        low_id, high_id = sorted([first_id, second_id])

        lock_low = _get_soldier_lock(low_id)
        lock_high = _get_soldier_lock(high_id)

        with lock_low:
            with lock_high:
                def _apply_fight():
                    fighters = {
                        f.pk: f
                        for f in Fighter.objects.filter(pk__in=[first_id, second_id])
                    }
                    first = fighters[first_id]
                    second = fighters[second_id]

                    FightTurn.objects.filter(fight=fight).delete()

                    if not events:
                        fight.is_stalemate = True
                        fight.winner = None
                        fight.is_simulated = True
                        fight.save(update_fields=["is_stalemate", "winner", "is_simulated"])
                        return fight.pk

                    turns_to_create = []
                    winner = None

                    for ev in events:
                        attacker = fighters[ev.attacker_id]
                        defender = fighters[ev.defender_id]
                        defender.health = max(0, defender.health - ev.damage)

                        turns_to_create.append(
                            FightTurn(
                                fight=fight,
                                turn_number=ev.step_number,
                                attacker=attacker,
                                defender=defender,
                                damage=ev.damage,
                                attacker_health=attacker.health,
                                defender_health=defender.health,
                            )
                        )
                        if defender.health == 0 and winner is None:
                            winner = attacker

                    first.save(update_fields=["health"])
                    second.save(update_fields=["health"])
                    FightTurn.objects.bulk_create(turns_to_create)

                    fight.winner = winner
                    fight.is_stalemate = False
                    fight.is_simulated = True
                    fight.save(update_fields=["winner", "is_stalemate", "is_simulated"])
                    return fight.pk

                return _run_db_write(_apply_fight)
    finally:
        close_old_connections()


def simulate_all_api_fights(reset_health: bool = False, max_workers: int = 8) -> dict:
    """Launch parallel background workers to replay all ingested API fights."""
    start_time = time.perf_counter()

    if reset_health:
        def _reset():
            Fighter.objects.filter(external_id__isnull=False).update(health=100)
            Fight.objects.filter(external_fight_id__isnull=False).update(is_simulated=False, winner=None)
        _run_db_write(_reset)

    target_qs = Fight.objects.filter(external_fight_id__isnull=False)
    if not reset_health and target_qs.filter(is_simulated=False).exists():
        target_qs = target_qs.filter(is_simulated=False)

    fight_ids = list(target_qs.values_list("pk", flat=True))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_replay_single_fight, fid) for fid in fight_ids]
        for future in as_completed(futures):
            future.result()

    elapsed = round(time.perf_counter() - start_time, 3)
    return {
        "replayed_count": len(fight_ids),
        "elapsed_seconds": elapsed,
        "workers": max_workers,
    }


def run_full_hard_pipeline(force_resync: bool = False) -> dict:
    """Ensure people, drafted soldiers, and fight events exist, then simulate all fights in parallel."""
    people_count = Person.objects.count()
    fights_count = Fight.objects.filter(external_fight_id__isnull=False).count()

    if force_resync or people_count == 0:
        people_count = sync_people()
        sync_and_draft_batches(max_workers=6)

    if force_resync or fights_count == 0:
        fights_count = sync_fight_events(max_workers=8)

    unsimulated_exists = Fight.objects.filter(
        external_fight_id__isnull=False, is_simulated=False
    ).exists()

    if force_resync or unsimulated_exists:
        sim_stats = simulate_all_api_fights(reset_health=force_resync, max_workers=8)
    else:
        sim_stats = {
            "replayed_count": 0,
            "elapsed_seconds": 0.0,
            "workers": 8,
        }

    return {
        "people_count": Person.objects.count(),
        "drafted_count": Fighter.objects.filter(external_id__isnull=False).count(),
        "fights_count": Fight.objects.filter(external_fight_id__isnull=False).count(),
        **sim_stats,
    }
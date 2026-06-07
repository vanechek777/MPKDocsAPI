"""
Жёсткие нагрузочные и отказоустойчивые проверки HTTP API «МПК.Документы».

Запуск из каталога MPKDocumentsAPI (сервер должен быть поднят):

    python -m app.Test
    python -m app.Test --test 2 --concurrency 120
    python -m app.Test --hard

Переменные окружения:
    MPK_API_BASE_URL      (по умолчанию http://localhost:8000)
    MPK_TEST_PHONE / MPK_TEST_PASSWORD
    MPK_HARD_MODE=1       — удвоенные лимиты нагрузки
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import httpx

BASE_URL = os.getenv("MPK_API_BASE_URL", "https://mpk-docs.ru.tuna.am").rstrip("/")
TEST_PHONE = os.getenv("MPK_TEST_PHONE", "+79148012594")
TEST_PASSWORD = os.getenv("MPK_TEST_PASSWORD", "password")
WRONG_PASSWORD = os.getenv("MPK_TEST_WRONG_PASSWORD", "wrong-password-load-test")
HARD_MODE = os.getenv("MPK_HARD_MODE", "").lower() in ("1", "true", "yes")

JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# --- Пороги (смягчаются при HARD_MODE через множитель нагрузки, не на пороги) ---
THRESHOLD = {
    "health_success_pct": 88.0 if HARD_MODE else 92.0,
    "health_p95_ms": 8000 if HARD_MODE else 5000,
    "login_success_pct": 85.0 if HARD_MODE else 90.0,
    "chaos_5xx_max": 0,
    "chaos_success_pct": 75.0 if HARD_MODE else 80.0,
    "soak_success_pct": 80.0 if HARD_MODE else 85.0,
    "recovery_min_ok": 8,
    "auth_hammer_success_pct": 70.0 if HARD_MODE else 75.0,
}


def _scale(n: int) -> int:
    return n * 2 if HARD_MODE else n


@dataclass
class LoadStats:
    name: str
    total: int
    ok: int
    failed: int
    status_counts: dict[int, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return (self.ok / self.total * 100.0) if self.total else 0.0

    @property
    def p95_ms(self) -> float | None:
        if not self.latencies_ms:
            return None
        s = sorted(self.latencies_ms)
        idx = max(0, int(len(s) * 0.95) - 1)
        return s[idx]

    def report(self) -> None:
        print(f"\n=== {self.name} ===")
        print(f"Запросов: {self.total}, успешно: {self.ok}, ошибок: {self.failed}")
        print(f"Успешность: {self.success_rate:.1f}%")
        if self.status_counts:
            print("Коды ответа:", dict(sorted(self.status_counts.items())))
        if self.errors:
            print("Сетевые/таймаут ошибки:", self.errors)
        if self.latencies_ms:
            s = sorted(self.latencies_ms)
            p50 = statistics.median(s)
            print(
                f"Задержка (мс): min={s[0]:.0f}, avg={statistics.mean(s):.0f}, "
                f"p50={p50:.0f}, p95={self.p95_ms:.0f}, max={s[-1]:.0f}"
            )


def _url(path: str) -> str:
    return f"{BASE_URL}/{path.lstrip('/')}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=_scale(250),
            max_keepalive_connections=_scale(120),
        ),
        timeout=httpx.Timeout(45.0, connect=12.0),
    )


def _record(
    stats: LoadStats,
    status: int,
    elapsed_ms: float,
    err: str | None,
    *,
    ok_statuses: set[int],
) -> None:
    stats.total += 1
    stats.latencies_ms.append(elapsed_ms)
    if err:
        stats.failed += 1
        stats.errors[err] = stats.errors.get(err, 0) + 1
        stats.status_counts[-1] = stats.status_counts.get(-1, 0) + 1
        return
    stats.status_counts[status] = stats.status_counts.get(status, 0) + 1
    if status in ok_statuses:
        stats.ok += 1
    else:
        stats.failed += 1


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 45.0,
) -> tuple[int, float, str | None]:
    t0 = time.perf_counter()
    try:
        res = await client.request(
            method,
            _url(path),
            json=json_body if content is None else None,
            content=content,
            headers=headers,
            timeout=timeout,
        )
        return res.status_code, (time.perf_counter() - t0) * 1000.0, None
    except httpx.TimeoutException:
        return 0, (time.perf_counter() - t0) * 1000.0, "timeout"
    except httpx.HTTPError as ex:
        return 0, (time.perf_counter() - t0) * 1000.0, type(ex).__name__


async def _run_parallel_named(
    name: str,
    n: int,
    worker: Callable[[httpx.AsyncClient, int], Awaitable[tuple[int, float, str | None]]],
    *,
    ok_statuses: set[int],
    shared_client: bool = True,
) -> LoadStats:
    stats = LoadStats(name=name, total=0, ok=0, failed=0)

    async def run_batch(client: httpx.AsyncClient) -> None:
        results = await asyncio.gather(*[worker(client, i) for i in range(n)])
        for status, ms, err in results:
            _record(stats, status, ms, err, ok_statuses=ok_statuses)

    if shared_client:
        async with _client() as client:
            await run_batch(client)
    else:
        # Отдельный клиент на запрос — давление на connect/handshake
        async def isolated(i: int) -> None:
            async with _client() as client:
                status, ms, err = await worker(client, i)
                _record(stats, status, ms, err, ok_statuses=ok_statuses)

        await asyncio.gather(*[isolated(i) for i in range(n)])

    return stats


async def _fetch_token(client: httpx.AsyncClient) -> str | None:
    res = await client.post(
        _url("/auth/login"),
        json={"phone_number": TEST_PHONE, "password": TEST_PASSWORD},
        headers=JSON_HEADERS,
        timeout=45.0,
    )
    if res.status_code != 200:
        return None
    return res.json().get("access_token")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **JSON_HEADERS}


# --- 1. Экстремальная нагрузка /health + отдельные TCP-соединения ---
async def test_1_health_extreme(concurrency: int | None = None) -> LoadStats:
    n = concurrency or _scale(200)

    async def worker(client: httpx.AsyncClient, _: int):
        return await _request(client, "GET", "/health", timeout=20.0)

    stats = await _run_parallel_named(
        f"1) Проверка GET /health: {n} параллельно (общий пул соединений)",
        n,
        worker,
        ok_statuses={200},
        shared_client=True,
    )
    stats.report()

    iso_n = min(60, n // 3)
    iso = await _run_parallel_named(
        f"1b) /health: {iso_n} запросов, каждый в своём клиенте (cold connect)",
        iso_n,
        worker,
        ok_statuses={200},
        shared_client=False,
    )
    iso.report()

    assert stats.success_rate >= THRESHOLD["health_success_pct"], (
        f"Успешность /health {stats.success_rate:.1f}% < {THRESHOLD['health_success_pct']}%"
    )
    p95 = stats.p95_ms
    if p95 is not None:
        assert p95 <= THRESHOLD["health_p95_ms"], f"p95={p95:.0f} мс превышает лимит"
    print("OK: /health выдержал проверку.")
    return stats


# --- 2. Тяжёлый вход + параллельная работа с JWT (тяжёлые SELECT) ---
async def test_2_auth_and_heavy_reads(concurrency: int | None = None) -> LoadStats:
    login_n = concurrency or _scale(60)
    read_n = _scale(80)

    async def login_worker(client: httpx.AsyncClient, _: int):
        return await _request(
            client,
            "POST",
            "/auth/login",
            json_body={"phone_number": TEST_PHONE, "password": TEST_PASSWORD},
            headers=JSON_HEADERS,
        )

    login_stats = await _run_parallel_named(
        f"2a) {login_n} параллельных POST /auth/login",
        login_n,
        login_worker,
        ok_statuses={200},
    )
    login_stats.report()
    assert login_stats.success_rate >= THRESHOLD["login_success_pct"]

    async with _client() as client:
        token = await _fetch_token(client)
        assert token, "Не удалось получить JWT для тяжёлого чтения"

        heavy_paths = [
            "/users/me",
            "/documents/recent?tab=all&limit=50",
            "/documents/recent?tab=received&limit=50",
            "/signing/inbox",
            "/templates",
        ]

        async def read_worker(c: httpx.AsyncClient, i: int):
            path = heavy_paths[i % len(heavy_paths)]
            return await _request(c, "GET", path, headers=_bearer(token), timeout=60.0)

        read_stats = await _run_parallel_named(
            f"2b) {read_n} параллельных тяжёлых GET с JWT",
            read_n,
            read_worker,
            ok_statuses={200},
        )
        read_stats.report()

        server_5xx = sum(
            read_stats.status_counts.get(c, 0) for c in (500, 502, 503, 504)
        )
        assert server_5xx == 0, f"5xx на защищённых маршрутах: {server_5xx}"
        assert read_stats.success_rate >= THRESHOLD["auth_hammer_success_pct"], (
            "Слишком много ошибок на чтении с валидным токеном"
        )

    print("OK: вход и параллельные тяжёлые чтения с JWT.")
    return read_stats


# --- 3. Адверсариал: мусор, огромные тела, битые токены (без 5xx) ---
async def test_3_adversarial_fault_tolerance(n: int | None = None) -> LoadStats:
    total = n or _scale(120)
    stats = LoadStats(
        name=f"3) Проверка на 'мусорные' запросы: {total} некорректных запросов",
        total=0,
        ok=0,
        failed=0,
    )

    async with _client() as client:
        tasks: list[Awaitable[tuple[int, float, str | None]]] = []

        for i in range(total):
            kind = i % 7
            if kind == 0:
                tasks.append(
                    _request(
                        client,
                        "POST",
                        "/auth/login",
                        json_body={"phone_number": TEST_PHONE, "password": WRONG_PASSWORD},
                        headers=JSON_HEADERS,
                    )
                )
            elif kind == 1:
                tasks.append(_request(client, "POST", "/auth/login", json_body={}, headers=JSON_HEADERS))
            elif kind == 2:
                huge = "Z" * 50_000
                tasks.append(
                    _request(
                        client,
                        "POST",
                        "/auth/login",
                        json_body={"phone_number": huge, "password": "x"},
                        headers=JSON_HEADERS,
                    )
                )
            elif kind == 3:
                tasks.append(
                    _request(
                        client,
                        "GET",
                        "/users/me",
                        headers={"Authorization": "Bearer " + "A" * 8000, **JSON_HEADERS},
                    )
                )
            elif kind == 4:
                tasks.append(
                    _request(
                        client,
                        "GET",
                        "/documents/recent?tab=all&limit=999999",
                        headers={"Authorization": "Bearer not.a.jwt", **JSON_HEADERS},
                    )
                )
            elif kind == 5:
                tasks.append(
                    _request(
                        client,
                        "POST",
                        "/auth/login",
                        content=b"{not-json",
                        headers={"Content-Type": "application/json"},
                    )
                )
            else:
                tasks.append(_request(client, "GET", "/admin/dashboard"))

        results = await asyncio.gather(*tasks)

    ok_statuses = {400, 401, 403, 404, 422, 405}
    for status, ms, err in results:
        _record(stats, status, ms, err, ok_statuses=ok_statuses)

    stats.report()
    server_5xx = sum(stats.status_counts.get(c, 0) for c in (500, 502, 503, 504))
    assert server_5xx <= THRESHOLD["chaos_5xx_max"], f"Сервер вернул 5xx: {server_5xx}"
    assert stats.success_rate >= THRESHOLD["chaos_success_pct"], (
        "Слишком много необработанных ответов на мусорные запросы"
    )
    print("OK: на мусор — 4xx, без падения сервера (5xx).")
    return stats


# --- 4. Хаос: случайная смесь нагрузки в течение N секунд ---
async def test_4_mixed_chaos_soak(duration_s: float | None = None, rps: int | None = None) -> LoadStats:
    duration = duration_s or (90.0 if HARD_MODE else 45.0)
    target_rps = rps or _scale(35)
    stats = LoadStats(
        name=f"4) Смешанный хаос {duration:.0f} с, ~{target_rps} req/s",
        total=0,
        ok=0,
        failed=0,
    )

    token: str | None = None
    async with _client() as client:
        token = await _fetch_token(client)

    ok_statuses = {200, 401, 403, 422}
    end = time.perf_counter() + duration
    sem = asyncio.Semaphore(_scale(80))
    pending: list[asyncio.Task[tuple[int, float, str | None]]] = []

    async def chaos_shot(client: httpx.AsyncClient) -> tuple[int, float, str | None]:
        roll = random.random()
        if roll < 0.35:
            return await _request(client, "GET", "/health", timeout=15.0)
        if roll < 0.55:
            return await _request(
                client,
                "POST",
                "/auth/login",
                json_body={"phone_number": TEST_PHONE, "password": WRONG_PASSWORD},
                headers=JSON_HEADERS,
            )
        if roll < 0.75 and token:
            path = random.choice(
                [
                    "/users/me",
                    "/documents/recent?tab=all&limit=20",
                    "/signing/inbox",
                ]
            )
            return await _request(client, "GET", path, headers=_bearer(token), timeout=45.0)
        if roll < 0.88 and token:
            return await _request(client, "GET", "/admin/dashboard", headers=_bearer(token))
        return await _request(client, "GET", "/documents/recent?tab=all&limit=10")

    async def bounded_shot() -> tuple[int, float, str | None]:
        async with sem:
            async with _client() as client:
                return await chaos_shot(client)

    interval = 1.0 / target_rps
    while time.perf_counter() < end:
        pending.append(asyncio.create_task(bounded_shot()))
        await asyncio.sleep(interval)

    results = await asyncio.gather(*pending, return_exceptions=True)
    for item in results:
        if isinstance(item, BaseException):
            _record(stats, 0, 0.0, type(item).__name__, ok_statuses=ok_statuses)
        else:
            status, ms, err = item
            _record(stats, status, ms, err, ok_statuses=ok_statuses)

    stats.report()
    server_5xx = sum(stats.status_counts.get(c, 0) for c in (500, 502, 503, 504))
    assert server_5xx <= max(3, int(stats.total * 0.02)), f"Слишком много 5xx в хаосе: {server_5xx}"
    assert stats.success_rate >= THRESHOLD["soak_success_pct"]
    print("OK: длительный хаос завершён, сервис не «лёг» массовыми 5xx.")
    return stats


# --- 5. Тройной всплеск + админ (если есть права) + жёсткое восстановление ---
async def test_5_triple_burst_admin_recovery(burst: int | None = None) -> LoadStats:
    burst_n = burst or _scale(150)
    stats = LoadStats(name="5) Тройной всплеск и восстановление", total=0, ok=0, failed=0)

    async def health_burst(label: str) -> LoadStats:
        async def w(c: httpx.AsyncClient, _: int):
            return await _request(c, "GET", "/health", timeout=12.0)

        s = await _run_parallel_named(label, burst_n, w, ok_statuses={200})
        s.report()
        return s

    waves = []
    for wave in range(3):
        print(f"\n--- Волна {wave + 1}/3 ---")
        waves.append(await health_burst(f"Волна {wave + 1}: {burst_n}× GET /health"))
        await asyncio.sleep(0.4 if wave < 2 else 1.5)

    async with _client() as client:
        token = await _fetch_token(client)
        if token:
            admin_n = _scale(40)
            print(f"\n--- Админ-нагрузка ({admin_n} запросов) ---")

            async def admin_worker(c: httpx.AsyncClient, i: int):
                path = random.choice(
                    [
                        "/admin/dashboard",
                        "/admin/activity?limit=50",
                        "/admin/users",
                    ]
                )
                return await _request(c, "GET", path, headers=_bearer(token), timeout=60.0)

            admin_stats = await _run_parallel_named(
                f"5b) Админ API × {admin_n}",
                admin_n,
                admin_worker,
                ok_statuses={200, 403},
            )
            admin_stats.report()
            if admin_stats.status_counts.get(200, 0) == 0 and admin_stats.status_counts.get(403, 0) > 0:
                print("Примечание: пользователь не админ — 403 ожидаемо.")
        else:
            print("Пропуск админ-нагрузки: нет JWT.")

    print("\n--- Контроль восстановления (15 быстрых /health) ---")
    recovery_ok = 0
    recovery_ms: list[float] = []
    async with _client() as client:
        for _ in range(15):
            status, ms, err = await _request(client, "GET", "/health", timeout=8.0)
            recovery_ms.append(ms)
            if err is None and status == 200:
                recovery_ok += 1

    print(f"Восстановление: {recovery_ok}/15 успешных, avg={statistics.mean(recovery_ms):.0f} мс")

    min_wave_ok = min(w.success_rate for w in waves)
    assert min_wave_ok >= 80.0, f"Одна из волн /health ниже 80%: {min_wave_ok:.1f}%"
    assert recovery_ok >= THRESHOLD["recovery_min_ok"], "Сервис не восстановился после тройного всплеска"
    print("OK: три волны нагрузки + контрольное восстановление пройдены.")
    return waves[0]


TESTS: dict[int, tuple[str, Callable[..., Awaitable[LoadStats]]]] = {
    1: ("Экстремальный /health", test_1_health_extreme),
    2: ("JWT + тяжёлые чтения", test_2_auth_and_heavy_reads),
    3: ("Адверсариал / мусор", test_3_adversarial_fault_tolerance),
    4: ("Смешанный хаос (soak)", test_4_mixed_chaos_soak),
    5: ("Тройной всплеск + recovery", test_5_triple_burst_admin_recovery),
}


async def run_all() -> None:
    print(f"Базовый URL: {BASE_URL}")
    print(f"Пользователь: {TEST_PHONE}")
    print(f"Режим HARD: {HARD_MODE}")
    print(f"Пороги: {THRESHOLD}")
    failures: list[str] = []

    for num, (title, fn) in TESTS.items():
        print("\n" + "=" * 70)
        try:
            await fn()
        except AssertionError as ex:
            failures.append(f"Тест {num} ({title}): {ex}")
            print(f"ПРОВАЛ: тест {num} — {ex}")
        except httpx.HTTPError as ex:
            failures.append(f"Тест {num}: нет связи с API — {ex}")
            print(f"ПРОВАЛ: тест {num} — сервер недоступен ({ex})")

    print("\n" + "=" * 70)
    if failures:
        print("Итог: провалы:")
        for f in failures:
            print(" -", f)
        raise SystemExit(1)
    print("Итог: все 5 жёстких проверок пройдены.")


def main() -> None:
    global HARD_MODE, THRESHOLD
    parser = argparse.ArgumentParser(description="Жёсткие нагрузочные тесты API")
    parser.add_argument("--test", type=int, choices=sorted(TESTS), help="Только один тест (1–5)")
    parser.add_argument("--concurrency", type=int, help="Параллелизм для тестов 1–2")
    parser.add_argument("--duration", type=float, help="Длительность теста 4 (сек)")
    parser.add_argument("--burst", type=int, help="Размер всплеска для теста 5")
    parser.add_argument(
        "--hard",
        action="store_true",
        help="Удвоить параллелизм и длительность хаоса",
    )
    args = parser.parse_args()

    if args.hard:
        HARD_MODE = True
        THRESHOLD = {
            "health_success_pct": 88.0,
            "health_p95_ms": 8000,
            "login_success_pct": 85.0,
            "chaos_5xx_max": 0,
            "chaos_success_pct": 75.0,
            "soak_success_pct": 80.0,
            "recovery_min_ok": 8,
            "auth_hammer_success_pct": 70.0,
        }

    if args.test:
        fn = TESTS[args.test][1]
        kwargs: dict[str, Any] = {}
        if args.test in (1, 2) and args.concurrency:
            kwargs["concurrency"] = args.concurrency
        if args.test == 4 and args.duration:
            kwargs["duration_s"] = args.duration
        if args.test == 5 and args.burst:
            kwargs["burst"] = args.burst
        asyncio.run(fn(**kwargs) if kwargs else fn())
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()

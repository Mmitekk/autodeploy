#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
  AutoDeploy — Автоматический деплой сайтов
=============================================================================

  Запуск:
    python3 autodeploy.py

  15 блоков деплоя. Большинство — автоматические.
  Пользователь вводит только: название проекта, путь, настройки БД,
  имя репозитория, токен GitHub.

=============================================================================
"""

import glob
import json
import os
import random
import socket
import string
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Callable, Dict, Any, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich import box
from rich.rule import Rule

console = Console()


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  УТИЛИТЫ                                                                 ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def run_cmd(cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    """Выполняет shell-команду. Возвращает (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Таймаут команды"
    except Exception as e:
        return -2, "", str(e)


def find_free_port(start: int = 3000, end: int = 9999, exclude: set = None) -> int:
    """Находит свободный порт в диапазоне."""
    exclude = exclude or set()
    _, out, _ = run_cmd("ss -tlnp 2>/dev/null | awk '{print $4}' | grep -oP '\\d+$' | sort -u")
    used_ports = set()
    for p in out.split('\n'):
        try:
            used_ports.add(int(p.strip()))
        except (ValueError, AttributeError):
            pass
    used_ports.update(exclude)

    for port in range(start, end):
        if port in used_ports:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.bind(('0.0.0.0', port))
            s.close()
            return port
        except (OSError, socket.error):
            continue
    return random.randint(49000, 59999)


def find_db_files(project_dir: str, max_depth: int = 3) -> List[str]:
    """Ищет *.db файлы в директории проекта до max_depth уровней."""
    results = []
    for depth in range(1, max_depth + 1):
        pattern = os.path.join(project_dir, *(['*'] * depth), '*.db')
        results.extend(glob.glob(pattern))
    results.extend(glob.glob(os.path.join(project_dir, '*.db')))
    results = sorted(set(results))
    db_subdir = [r for r in results if '/db/' in r]
    other = [r for r in results if '/db/' not in r]
    return db_subdir + other


def generate_password(length: int = 20) -> str:
    """Генерирует случайный пароль."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))


def get_site_domain(site_path: str) -> str:
    """Извлекает домен из пути к сайту."""
    return os.path.basename(site_path.rstrip('/'))


def domain_to_punycode(domain: str) -> str:
    """Конвертирует кириллический/IDN домен в Punycode для Nginx и DNS.

    Nginx не понимает кириллицу в server_name — ему нужен xn-- формат.
    Браузеры автоматически конвертируют кириллические домены в Punycode
    в HTTP-заголовке Host, поэтому Nginx должен матчить именно Punycode.

    Примеры:
        пункт-службы.рф → xn------...xn--p1ai
        example.com → example.com (без изменений)
    """
    try:
        # Python 3 встроенная IDNA-конвертация
        return domain.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        # Если не удалось — возвращаем как есть
        return domain


def slugify_name(name: str) -> str:
    """Конвертирует имя проекта в безопасный ASCII-слаг для PM2.

    PM2 использует имя процесса для внутренних файлов в ~/.pm2/,
    поэтому кириллица и спецсимволы могут ломать работу.
    Конвертируем в Punycode, убираем точки и лишние дефисы.

    Примеры:
        пункт-службы-по-контракту.рф → xn------8cdc3a0afbdtikcehwhrmdcgo1q-xn--p1ai
        be10st.ru → be10st-ru
        my-site → my-site
    """
    # Сначала конвертируем кириллицу в Punycode
    try:
        slug = name.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        slug = name

    # Заменяем точки на дефисы (PM2 не любит точки в именах)
    slug = slug.replace('.', '-')

    # Убираем множественные дефисы
    import re
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')

    return slug or 'app'


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  РЕЗУЛЬТАТ БЛОКА                                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

@dataclass
class BlockResult:
    block_id: str
    block_num: int
    title: str
    success: bool
    message: str = ""
    details: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ИСПОЛНИТЕЛЬ БЛОКОВ — ФАКТИЧЕСКАЯ ЛОГИКА ДЕПЛОЯ                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class DeployExecutor:
    """Исполнитель блоков деплоя."""

    def __init__(self, context: Dict[str, Any]):
        self.ctx = context
        self.results: List[BlockResult] = []
        self.used_ports: set = set()

    def _add_result(self, block_num: int, block_id: str, title: str,
                    success: bool, message: str = "", details: str = "",
                    data: Dict = None) -> BlockResult:
        r = BlockResult(
            block_id=block_id, block_num=block_num, title=title,
            success=success, message=message, details=details, data=data or {}
        )
        self.results.append(r)
        return r

    # ── Блок 1: Проект и путь ────────────────────────────────────────────

    def execute_block1(self) -> BlockResult:
        project_name = self.ctx.get("project_name", "").strip()
        site_path = self.ctx.get("site_path", "").strip()

        if not project_name:
            return self._add_result(1, "project", "Название проекта и путь", False,
                                    "Название проекта не указано")
        if not site_path:
            return self._add_result(1, "project", "Название проекта и путь", False,
                                    "Путь к сайту не указан")

        # Определяем домен из пути и конвертируем в Punycode если кириллица
        raw_domain = get_site_domain(site_path)
        punycode_domain = domain_to_punycode(raw_domain)
        is_idn = raw_domain != punycode_domain  # True если домен кириллический

        # ВАЖНО: Для кириллических доменов site_path ДОЛЖЕН быть в Punycode!
        # Кириллица в путях ломает PM2, npm и другие инструменты.
        # Пример: /var/www/пункт.рф → /var/www/xn----abc.xn--p1ai
        if is_idn:
            # Заменяем кириллический домен в пути на Punycode
            # site_path = /some/path/кириллица.рф → /some/path/xn----abc.xn--p1ai
            site_path_punycode = site_path.rstrip('/')[:-len(raw_domain)] + punycode_domain

            # Проверяем, какая директория существует на диске
            # (пользователь мог создать punycode-директорию вручную)
            if os.path.exists(site_path_punycode) and not os.path.exists(site_path):
                site_path = site_path_punycode
            elif not os.path.exists(site_path) and not os.path.exists(site_path_punycode):
                # Ни одной не существует — создаём punycode-путь (ASCII-безопасный)
                site_path = site_path_punycode
            elif os.path.exists(site_path) and not os.path.exists(site_path_punycode):
                # Кириллическая директория существует, Punycode — нет
                # Рекомендуем переименовать, но не заставляем
                console.print(
                    f"  [yellow]Внимание: директория {site_path} — кириллическая.[/yellow]"
                )
                console.print(
                    f"  [yellow]Кириллица в путях может ломать PM2, npm, certbot.[/yellow]"
                )
                console.print(
                    f"  [yellow]Рекомендуется переименовать: mv '{site_path}' '{site_path_punycode}'[/yellow]"
                )
                # Переименовываем автоматически если можем
                try:
                    os.rename(site_path, site_path_punycode)
                    site_path = site_path_punycode
                    console.print(f"  [green]Директория переименована в {site_path_punycode}[/green]")
                except OSError:
                    # Не смогли — используем как есть
                    pass
        else:
            site_path_punycode = site_path

        # PM2-имя: безопасный ASCII-слаг (кириллица ломает PM2)
        pm2_name = slugify_name(project_name)

        # ── Проверка конфликтов PM2 и «похожих» директорий ──────────────
        # Если PM2-процесс с таким именем уже существует, но указывает
        # на другую директорию (например, /var/www/git.be1st.pro вместо
        # /var/www/git-be1st.pro), нужно его пересоздать.
        # Это случается когда: домен содержит точку → slugify заменяет на дефис,
        # но старая директория (с точкой) тоже существует.
        _pm2_conflict_fixed = False
        try:
            rc_pm2, out_pm2, _ = run_cmd(f"pm2 describe {pm2_name} 2>/dev/null")
            if rc_pm2 == 0 and out_pm2:
                # Ищем exec cwd в выводе pm2 describe
                import re as _re
                _cwd_match = _re.search(r'exec cwd\s+│\s+(\S+)', out_pm2)
                if _cwd_match:
                    _existing_cwd = _cwd_match.group(1).rstrip('/')
                    _expected_cwd = site_path.rstrip('/')
                    if _existing_cwd != _expected_cwd:
                        console.print(
                            f"  [yellow]⚠ Конфликт PM2: процесс '{pm2_name}' "
                            f"указывает на {_existing_cwd}, а сайт — на {_expected_cwd}[/yellow]"
                        )
                        console.print(
                            f"  [cyan]Удаляю конфликтующий PM2-процесс...[/cyan]"
                        )
                        run_cmd(f"pm2 delete {pm2_name} 2>/dev/null")
                        run_cmd(f"pm2 delete {pm2_name}-webhook 2>/dev/null")
                        run_cmd("pm2 save 2>/dev/null")
                        _pm2_conflict_fixed = True
                        console.print(
                            f"  [green]Конфликтующий PM2-процесс удалён[/green]"
                        )
        except Exception:
            pass

        # Проверяем «похожие» директории (точка ↔ дефис)
        # git.be1st.pro ↔ git-be1st.pro — разные пути, одинаковый PM2-слаг
        _similar_dirs = []
        _site_basename = os.path.basename(site_path.rstrip('/'))
        for _variant in [_site_basename.replace('.', '-'), _site_basename.replace('-', '.')]:
            _variant_path = os.path.join(os.path.dirname(site_path.rstrip('/')), _variant)
            if _variant_path.rstrip('/') != site_path.rstrip('/') and os.path.isdir(_variant_path):
                _similar_dirs.append(_variant_path)
        if _similar_dirs:
            console.print(
                f"  [yellow]⚠ Обнаружены директории с похожими именами:[/yellow]"
            )
            for _sd in _similar_dirs:
                console.print(f"  [yellow]  • {_sd}[/yellow]")
            console.print(
                f"  [yellow]PM2-слаг у них одинаковый ({pm2_name}) — "
                f"это может вызывать конфликты![/yellow]"
            )
            console.print(
                f"  [yellow]Рекомендуется удалить неиспользуемую директорию.[/yellow]"
            )

        if os.path.exists(site_path):
            self.ctx["site_path"] = site_path
            self.ctx["project_name"] = project_name
            self.ctx["pm2_name"] = pm2_name
            self.ctx["domain"] = punycode_domain
            self.ctx["domain_display"] = raw_domain  # Оригинал для отображения
            self.ctx["domain_is_idn"] = is_idn
            details = f"Путь: {site_path}\nДомен: {raw_domain}\nPM2 имя: {pm2_name}"
            if is_idn:
                details += f"\nДомен (Punycode): {punycode_domain}"
            return self._add_result(1, "project", "Название проекта и путь", True,
                                    f"Проект: {project_name}", details)
        else:
            try:
                os.makedirs(site_path, exist_ok=True)
                self.ctx["site_path"] = site_path
                self.ctx["project_name"] = project_name
                self.ctx["pm2_name"] = pm2_name
                self.ctx["domain"] = punycode_domain
                self.ctx["domain_display"] = raw_domain
                self.ctx["domain_is_idn"] = is_idn
                details = f"Путь: {site_path}\nДомен: {raw_domain}\nPM2 имя: {pm2_name}"
                if is_idn:
                    details += f"\nДомен (Punycode): {punycode_domain}"
                return self._add_result(1, "project", "Название проекта и путь", True,
                                        f"Проект: {project_name} (путь создан)", details)
            except Exception as e:
                return self._add_result(1, "project", "Название проекта и путь", False,
                                        f"Не удалось создать путь: {site_path}", str(e))

    # ── Блок 2: Свободный порт (авто) ────────────────────────────────────

    def execute_block2(self) -> BlockResult:
        port = find_free_port(3000, 9999, self.used_ports)
        self.ctx["app_port"] = port
        self.used_ports.add(port)
        return self._add_result(2, "port_select", "Выбор свободного порта", True,
                                f"Порт: {port}")

    # ── Блок 3: Режим запуска (Standalone / Standard) ──────────────────

    def execute_block3(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        use_standalone = self.ctx.get("use_standalone", "auto")

        # Автодетект standalone в next.config
        has_standalone_config = False
        for cfg in ["next.config.ts", "next.config.js", "next.config.mjs"]:
            cfg_path = os.path.join(site_path, cfg)
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, 'r') as f:
                        content = f.read()
                    if 'standalone' in content:
                        has_standalone_config = True
                        break
                except Exception:
                    pass

        # Проверяем .next/standalone/server.js
        has_standalone_build = False
        for candidate in [
            os.path.join(site_path, ".next", "standalone", "server.js"),
        ]:
            if os.path.exists(candidate):
                has_standalone_build = True
                break
        # Также ищем рекурсивно
        if not has_standalone_build:
            standalone_dir = os.path.join(site_path, ".next", "standalone")
            if os.path.isdir(standalone_dir):
                for root, dirs, files in os.walk(standalone_dir):
                    if "server.js" in files:
                        has_standalone_build = True
                        break

        if use_standalone == "1" or use_standalone == "standalone":
            self.ctx["use_standalone"] = "yes"
            mode_desc = "Standalone (node server.js)"
        elif use_standalone == "2" or use_standalone == "standard":
            self.ctx["use_standalone"] = "no"
            mode_desc = "Standard (npx next start)"
        else:
            # Авто
            if has_standalone_config or has_standalone_build:
                self.ctx["use_standalone"] = "yes"
                mode_desc = "Standalone (автоопределение)"
            else:
                self.ctx["use_standalone"] = "no"
                mode_desc = "Standard (автоопределение)"

        details = []
        if has_standalone_config:
            details.append("Обнаружен output: standalone в конфиге")
        if has_standalone_build:
            details.append("Обнаружен .next/standalone/server.js")

        return self._add_result(3, "run_mode", "Режим запуска", True,
                                mode_desc,
                                '\n'.join(details) if details else "")

    # ── Блок 4: Домен и SSL ─────────────────────────────────────────────

    def execute_block4(self) -> BlockResult:
        site_domain = self.ctx.get("site_domain", "").strip()
        use_ssl = self.ctx.get("use_ssl", "no")

        if not site_domain:
            site_domain = self.ctx.get("domain", "")

        # Очищаем домен: убираем протокол, путь, trailing slash
        # Пользователь может ввести: https://пункт.рф/ или http://site.ru/page
        import re
        site_domain = re.sub(r'^https?://', '', site_domain)
        site_domain = site_domain.split('/')[0]
        site_domain = site_domain.rstrip(':')

        # Конвертируем в Punycode для системных конфигов (Nginx, DNS)
        # Оригинал сохраняем для отображения и URL
        domain_display = site_domain
        site_domain_punycode = domain_to_punycode(site_domain)
        is_idn = site_domain != site_domain_punycode

        # site_domain — Punycode (для Nginx, DNS, curl)
        self.ctx["site_domain"] = site_domain_punycode
        # domain_display — оригинал (для URL, отображения)
        self.ctx["domain_display"] = domain_display
        self.ctx["use_ssl"] = use_ssl == "yes"

        if site_domain_punycode:
            display_text = domain_display
            if is_idn:
                display_text += f" ({site_domain_punycode})"
            return self._add_result(4, "domain_ssl", "Домен и SSL", True,
                                    f"Домен: {display_text}, SSL: {'Да' if use_ssl == 'yes' else 'Нет'}")
        else:
            return self._add_result(4, "domain_ssl", "Домен и SSL", True,
                                    "Домен не указан — Apache не настраивается")

    # ── Блок 5: PM2 (авто) ───────────────────────────────────────────────

    def execute_block5(self) -> BlockResult:
        project_name = self.ctx.get("project_name", "app")
        pm2_name = self.ctx.get("pm2_name", slugify_name(project_name))
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)
        use_standalone = self.ctx.get("use_standalone", "no")

        rc, _, _ = run_cmd("which pm2")
        if rc != 0:
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", False,
                                    "pm2 не установлен",
                                    "Установите: npm install -g pm2")

        # Определяем скрипт запуска в зависимости от режима
        if use_standalone == "yes":
            pm2_script = "node"
            pm2_args = ".next/standalone/server.js"
            mode_label = "Standalone"
        else:
            # ВАЖНО: Используем 'npx next start -p PORT' вместо 'npm start'!
            # Шаблоны Next.js часто хардкодят порт в package.json:
            #   "start": "next start -p 3000"
            # Флаг -p ПРИОРИТЕТНЕЕ env-переменной PORT, поэтому при
            # запуске через 'npm start' приложение стартует на порту 3000,
            # а не на назначенном скриптом порту. npx next start -p PORT
            # гарантирует, что приложение слушает правильный порт.
            pm2_script = "npx"
            pm2_args = f"next start -p {app_port}"
            mode_label = "Standard"

        # Сохраняем параметры запуска для Block 11
        self.ctx["pm2_script"] = pm2_script
        self.ctx["pm2_args"] = pm2_args
        self.ctx["pm2_mode_label"] = mode_label

        ecosystem_content = f"""module.exports = {{
  apps: [{{
    name: '{pm2_name}',
    script: '{pm2_script}',
    args: '{pm2_args}',
    cwd: '{site_path}',
    env: {{
      PORT: {app_port},
      NODE_ENV: '{self.ctx.get("deploy_mode", "production").lower()}',
      HOSTNAME: '0.0.0.0'
    }},
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    error_file: '{site_path}/logs/pm2-error.log',
    out_file: '{site_path}/logs/pm2-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }}]
}};
"""
        ecosystem_path = os.path.join(site_path, "ecosystem.config.js")
        # Снимаем immutable-атрибут, если установлен (chattr +i от предыдущего деплоя)
        run_cmd(f"chattr -i {ecosystem_path} 2>/dev/null")
        try:
            os.makedirs(os.path.dirname(ecosystem_path), exist_ok=True)
            with open(ecosystem_path, 'w') as f:
                f.write(ecosystem_content)
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", True,
                                    f"Конфиг создан: {mode_label}",
                                    f"Режим: {mode_label}\nПорт: {app_port}\nФайл: {ecosystem_path}")
        except Exception as e:
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", False,
                                    f"Ошибка: {e}")

    # ── Блок 6: Веб-сервер (авто) ──────────────────────────────────────

    def _detect_port80_server(self) -> str:
        """Определяет, какой веб-сервер слушает порт 80."""
        rc, out, _ = run_cmd("ss -tlnp 'sport = :80' 2>/dev/null")
        if rc == 0 and out:
            if 'nginx' in out.lower():
                return "nginx"
            if 'apache' in out.lower() or 'httpd' in out.lower():
                return "apache"
        # Проверяем процессы
        rc, out, _ = run_cmd("ps aux | grep -E '[n]ginx|[a]pache2|[h]ttpd' | head -5")
        if rc == 0 and out:
            if 'nginx' in out.lower():
                return "nginx"
            if 'apache' in out.lower() or 'httpd' in out.lower():
                return "apache"
        # Fallback: наличие директорий
        if os.path.isdir("/etc/nginx/sites-enabled") or os.path.isdir("/etc/nginx/conf.d"):
            return "nginx"
        if os.path.isdir("/etc/apache2/sites-enabled") or os.path.isdir("/etc/httpd/conf.d"):
            return "apache"
        return "unknown"

    def _config_has_proxy(self, config_path: str, port: int) -> bool:
        """Проверяет, содержит ли конфиг прокси на нужный порт."""
        try:
            with open(config_path, 'r') as f:
                content = f.read().lower()
            return str(port) in content and (
                'proxypass' in content or 'proxy_pass' in content
            )
        except Exception:
            return False

    def _update_nginx_config(self, domain: str, site_path: str, app_port: int) -> BlockResult:
        """Создаёт или обновляет конфиг Nginx.
        
        Автоматически определяет стиль дистрибутива:
        - RHEL (Alma Linux, CentOS, Rocky): /etc/nginx/conf.d/{domain}.conf
        - Debian/Ubuntu: /etc/nginx/sites-available/{domain} + симлинк в sites-enabled/
        """
        # Автоопределение: RHEL использует conf.d/, Debian — sites-available/
        is_rhel = os.path.isdir("/etc/nginx/conf.d")
        is_debian = os.path.isdir("/etc/nginx/sites-available")

        if is_rhel:
            nginx_sites = "/etc/nginx/conf.d"
            config_path = os.path.join(nginx_sites, f"{domain}.conf")
            needs_symlink = False
        elif is_debian:
            nginx_sites = "/etc/nginx/sites-available"
            nginx_enabled = "/etc/nginx/sites-enabled"
            config_path = os.path.join(nginx_sites, domain)
            needs_symlink = True
        else:
            # Если ни один не найден — создаём conf.d/
            nginx_sites = "/etc/nginx/conf.d"
            os.makedirs(nginx_sites, exist_ok=True)
            config_path = os.path.join(nginx_sites, f"{domain}.conf")
            needs_symlink = False

        # Переопределяем nginx_enabled для Debian-ветки
        if is_debian:
            nginx_enabled = "/etc/nginx/sites-enabled"
        else:
            nginx_enabled = None
        existing_proxy = False
        has_ssl = False

        if os.path.exists(config_path):
            existing_proxy = self._config_has_proxy(config_path, app_port)
            # Проверяем, есть ли SSL-секция от certbot — если да, не перезаписываем!
            try:
                with open(config_path, 'r') as f:
                    existing_content = f.read()
                has_ssl = 'listen 443 ssl' in existing_content or 'listen [::]:443 ssl' in existing_content
            except Exception:
                pass

        if existing_proxy and has_ssl:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Nginx: конфиг уже настроен с SSL ({config_path})",
                                    f"Прокси на порт {app_port}")

        if existing_proxy and not has_ssl:
            # Прокси есть, но порт мог измениться — обновляем только proxy_pass
            import re
            new_content = re.sub(
                r'proxy_pass http://127\.0\.0\.1:\d+',
                f'proxy_pass http://127.0.0.1:{app_port}',
                existing_content
            )
            # Обновляем alias для static
            new_content = re.sub(
                r'alias /var/www/[^;]+/\.next/static/',
                f'alias {site_path}/.next/static/',
                new_content
            )
            try:
                with open(config_path, 'w') as f:
                    f.write(new_content)
                rc_test, _, test_err = run_cmd("nginx -t 2>&1")
                if rc_test == 0:
                    run_cmd("systemctl reload nginx")
                return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                        f"Nginx: конфиг обновлён ({config_path})",
                                        f"Прокси на порт {app_port}")
            except Exception as e:
                return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                        f"Ошибка обновления конфига: {e}")

        # Создаём конфиг с нуля (SSL добавит certbot позже)
        vhost = f"""server {{
    listen 80;
    server_name {domain} www.{domain};

    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }}

    location /_next/static/ {{
        alias {site_path}/.next/static/;
        expires 365d;
        access_log off;
    }}

    error_log /var/log/nginx/{domain}_error.log;
    access_log /var/log/nginx/{domain}_access.log;
}}
"""
        try:
            with open(config_path, 'w') as f:
                f.write(vhost)
            # Симлинк — только для Debian-style
            if needs_symlink and nginx_enabled:
                enabled_link = os.path.join(nginx_enabled, domain)
                if not os.path.exists(enabled_link):
                    run_cmd(f"ln -sf {config_path} {enabled_link}")
                # Удаляем default если мешает
                default_conf = os.path.join(nginx_enabled, "default")
                if os.path.exists(default_conf):
                    run_cmd(f"mv {default_conf} {default_conf}.bak 2>/dev/null")

            # Для длинных доменов (Punycode) увеличиваем hash_bucket_size
            # Иначе nginx не сможет обработать server_name: "could not build
            # server_names_hash, you should increase server_names_hash_bucket_size"
            nginx_main_conf = "/etc/nginx/nginx.conf"
            rc_grep, grep_out, _ = run_cmd(
                f"grep -c 'server_names_hash_bucket_size' {nginx_main_conf}"
            )
            if rc_grep != 0 or not grep_out.strip() or grep_out.strip() == "0":
                # Добавляем в блок http { ... }
                run_cmd(
                    f"sed -i '/http {{/a \\\\tserver_names_hash_bucket_size 128;' {nginx_main_conf}"
                )

            # Проверяем конфиг перед reload — не ломаем все сайты
            rc_test, _, test_err = run_cmd("nginx -t 2>&1")
            if rc_test == 0:
                run_cmd("systemctl reload nginx")
            else:
                return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                        f"Nginx конфиг ошибочен: {test_err[:200]}")

            action = "Обновлён" if existing_proxy else "Создан"
            distro = "RHEL (conf.d)" if is_rhel else "Debian (sites-available)"
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Nginx: {action} конфиг для {domain}",
                                    f"Прокси на порт {app_port}\nПуть: {config_path}\nСтиль: {distro}")
        except Exception as e:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    f"Ошибка Nginx: {e}")

    def _update_apache_config(self, domain: str, site_path: str, app_port: int) -> BlockResult:
        """Создаёт или обновляет конфиг Apache."""
        apache_sites = "/etc/apache2/sites-available"
        apache_enabled = "/etc/apache2/sites-enabled"

        config_path = os.path.join(apache_sites, f"{domain}.conf")
        existing_proxy = False

        if os.path.exists(config_path):
            existing_proxy = self._config_has_proxy(config_path, app_port)

        if existing_proxy:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Apache: конфиг уже настроен ({config_path})",
                                    f"Прокси на порт {app_port}")

        # Создаём / перезаписываем конфиг
        vhost = f"""<VirtualHost *:80>
    ServerName {domain}
    ServerAlias www.{domain}
    DocumentRoot {site_path}/public

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:{app_port}/
    ProxyPassReverse / http://127.0.0.1:{app_port}/

    <Directory {site_path}/public>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog ${{APACHE_LOG_DIR}}/{domain}_error.log
    CustomLog ${{APACHE_LOG_DIR}}/{domain}_access.log combined
</VirtualHost>
"""
        try:
            with open(config_path, 'w') as f:
                f.write(vhost)
            run_cmd(f"a2ensite {domain}.conf 2>/dev/null")
            # Включаем proxy-модули
            run_cmd("a2enmod proxy proxy_http 2>/dev/null")
            run_cmd("systemctl reload apache2")
            action = "Обновлён" if os.path.exists(config_path) else "Создан"
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Apache: {action} конфиг для {domain}",
                                    f"Прокси на порт {app_port}")
        except Exception as e:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    f"Ошибка Apache: {e}")

    def _setup_ssl_certbot(self, domain: str, port80_server: str) -> Tuple[bool, str]:
        """Настраивает SSL через certbot. Возвращает (success, message)."""
        # Проверяем, установлен ли certbot
        rc, _, _ = run_cmd("which certbot")
        if rc != 0:
            console.print("  [cyan]Установка certbot...[/cyan]")
            rc, _, err = run_cmd(
                "apt-get update -qq && apt-get install -y -qq certbot python3-certbot-nginx",
                timeout=180
            )
            if rc != 0:
                return False, f"certbot не установлен: {err[:150]}"

        # Выбираем плагин certbot в зависимости от веб-сервера
        if port80_server == "nginx":
            plugin = "--nginx"
        elif port80_server == "apache":
            plugin = "--apache"
        else:
            # Для неизвестного сервера пробуем standalone режим
            # (нужно временно остановить веб-сервер)
            plugin = "standalone --pre-hook 'systemctl stop nginx' --post-hook 'systemctl start nginx'"

        # Проверяем, есть ли уже сертификат
        rc_check, out_check, _ = run_cmd(
            f"certbot certificates -d {domain} 2>/dev/null | grep -c 'Certificate Name'"
        )
        if rc_check == 0 and out_check.strip() != "0":
            # Сертификат уже есть — просто обновляем конфиг
            console.print("  [cyan]SSL сертификат уже существует, обновляю конфиг...[/cyan]")
            if port80_server == "nginx":
                run_cmd("systemctl reload nginx")
            elif port80_server == "apache":
                run_cmd("systemctl reload apache2")
            return True, "SSL сертификат уже существует, конфиг обновлён"

        # Запускаем certbot
        console.print(f"  [cyan]Получаю SSL сертификат для {domain}...[/cyan]")
        rc, out, err = run_cmd(
            f"certbot {plugin} -d {domain} -d www.{domain} "
            f"--non-interactive --agree-tos "
            f"--register-unsafely-without-email --redirect "
            f"--keep-until-expiring",
            timeout=120
        )

        if rc == 0:
            # Настраиваем автоматическое обновление
            # certbot renew запускается через systemd timer или cron
            run_cmd("systemctl enable certbot.timer 2>/dev/null || true")
            run_cmd("systemctl start certbot.timer 2>/dev/null || true")
            return True, "SSL сертификат получен, HTTPS настроен с редиректом"
        else:
            err_text = (err or out or "")[-300:]
            # DNS может ещё не резолвиться — это нормально при первом деплое
            hint = (
                f"certbot вернул ошибку. Возможная причина: DNS ещё не резолвится. "
                f"Выполните вручную позже:\n"
                f"  certbot {plugin} -d {domain} -d www.{domain}"
            )
            return False, f"{err_text}\n{hint}"

    def execute_block6(self) -> BlockResult:
        domain = self.ctx.get("domain", "")
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)

        if not domain:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    "Домен не определён")

        # Сохраняем информацию о веб-сервере
        port80_server = self._detect_port80_server()
        self.ctx["port80_server"] = port80_server

        # Настраиваем фронтенд-сервер (тот что слушает порт 80)
        if port80_server == "nginx":
            result = self._update_nginx_config(domain, site_path, app_port)
        elif port80_server == "apache":
            result = self._update_apache_config(domain, site_path, app_port)
        else:
            # Не определили — пробуем оба
            # RHEL: /etc/nginx/conf.d/  |  Debian: /etc/nginx/sites-available/
            if os.path.isdir("/etc/nginx/conf.d") or os.path.isdir("/etc/nginx/sites-available"):
                result = self._update_nginx_config(domain, site_path, app_port)
            elif os.path.isdir("/etc/apache2/sites-available") or os.path.isdir("/etc/httpd/conf.d"):
                result = self._update_apache_config(domain, site_path, app_port)
            else:
                result = self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                          "Ни Apache, ни Nginx не найдены")

        # SSL через certbot (после создания HTTP-конфига)
        if self.ctx.get("use_ssl", False) and result.success:
            ssl_ok, ssl_msg = self._setup_ssl_certbot(domain, port80_server)
            if ssl_ok:
                result.message += " + SSL"
                result.details += f"\n{ssl_msg}"
            else:
                # SSL не критичен — сайт работает и без него
                result.details += f"\n⚠ SSL не настроен: {ssl_msg}"
                console.print(f"  [yellow]⚠ SSL не настроен: {ssl_msg[:100]}[/yellow]")

        return result

    # ── Блок 7: Репозиторий ──────────────────────────────────────────────

    def execute_block7(self) -> BlockResult:
        repo_name = self.ctx.get("repo_name", "").strip()
        github_token = self.ctx.get("github_token", "").strip()
        repo_private = self.ctx.get("repo_private", True)

        if not repo_name:
            return self._add_result(7, "git_repo", "Git-репозиторий", False,
                                    "Имя репозитория не указано")

        self.ctx["repo_name"] = repo_name
        self.ctx["github_token"] = github_token
        self.ctx["repo_private"] = repo_private

        details = f"Токен: {'указан' if github_token else 'не указан'}\n"
        details += f"Приватный: {'да' if repo_private else 'нет'}"

        return self._add_result(7, "git_repo", "Git-репозиторий", True,
                                f"Репозиторий: {repo_name}",
                                details)

    # ── Блок 8: Поиск *.db (авто) ────────────────────────────────────────

    def execute_block8(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        db_files = find_db_files(site_path, max_depth=3)

        if db_files:
            selected = db_files[0]
            self.ctx["db_file"] = selected
            rel_path = os.path.relpath(selected, site_path)
            return self._add_result(8, "db_file_find", "Поиск файла базы данных", True,
                                    f"Найдена БД: {rel_path}",
                                    f"Полный путь: {selected}\nВсего *.db: {len(db_files)}",
                                    data={"db_file": selected, "all_db_files": db_files})
        else:
            self.ctx["db_file"] = ""
            return self._add_result(8, "db_file_find", "Поиск файла базы данных", True,
                                    "*.db файлы не найдены",
                                    "База данных не обнаружена в директории проекта",
                                    data={"db_file": "", "all_db_files": []})

    # ── Блок 9: .gitignore (авто) ────────────────────────────────────────

    def execute_block9(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")

        # .gitignore как в оригинальном скрипте
        entries = [
            "node_modules/",
            ".next/",
            "out/",
            "db/",
            "*.db",
            "*.db-journal",
            "*.db-wal",
            "*.db-shm",
            ".env",
            ".env.local",
            ".env.*.local",
            # Загруженные пользователями файлы — НЕ в git!
            # При git reset --hard они затираются.
            # public/uploads/ должна содержать только серверные файлы.
            "public/uploads/",
            "*.log",
            "npm-debug.log*",
            ".DS_Store",
            "Thumbs.db",
            "*.tmp",
            "*.bak",
            "*.swp",
            ".vscode/",
            ".idea/",
            "*.pem",
            "*.key",
            "*.crt",
            "setup-git-deploy.sh",
            "webhook-server.js",
            "deploy.sh",
            ".env.webhook",
            "ecosystem.config.js",
            "logs/",
        ]

        gitignore_path = os.path.join(site_path, ".gitignore")
        try:
            os.makedirs(os.path.dirname(gitignore_path), exist_ok=True)
            with open(gitignore_path, 'w') as f:
                f.write('\n'.join(entries) + '\n')
            return self._add_result(9, "gitignore", "Создание .gitignore", True,
                                    f"Создан: {gitignore_path}")
        except Exception as e:
            return self._add_result(9, "gitignore", "Создание .gitignore", False,
                                    f"Ошибка: {e}")

    # ── Блок 10: .env (авто) ─────────────────────────────────────────────

    def execute_block10(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)
        db_file = self.ctx.get("db_file", "")
        site_domain = self.ctx.get("site_domain", "")
        use_ssl = self.ctx.get("use_ssl", False)
        project_name = self.ctx.get("project_name", "app")

        # Читаем существующий .env если есть — сохраняем пользовательские переменные
        existing_env = {}
        env_path = os.path.join(site_path, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            existing_env[k.strip()] = v.strip()
            except Exception:
                pass

        # Формируем .env с SQLite (как в оригинальном скрипте)
        env_lines = [
            "# Application",
            f"APP_NAME={project_name}",
            f"APP_PORT={app_port}",
            f"NODE_ENV=production",
            "",
            "# Server",
            f"HOST=0.0.0.0",
            f"PORT={app_port}",
        ]

        # Database — SQLite
        # Важно: DATABASE_URL должен быть АБСОЛЮТНЫМ путём,
        # т.к. standalone-режим запускается из .next/standalone/
        # и относительный путь ./db/ будет искаться не там
        if db_file:
            # Убеждаемся, что путь абсолютный
            if not os.path.isabs(db_file):
                db_file = os.path.abspath(os.path.join(site_path, db_file))
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f"DATABASE_URL=\"file:{db_file}\"",
                f"DB_FILE={db_file}",
            ])
        else:
            # Пробуем найти db/custom.db (стандартный путь в шаблонах)
            # Создаём директорию db/ если нет — DATABASE_URL должен указывать
            # на существующую директорию, иначе Prisma упадёт при migrate
            db_dir = os.path.join(site_path, "db")
            os.makedirs(db_dir, exist_ok=True)

            db_abs = os.path.join(site_path, "db", "custom.db")
            if not os.path.exists(db_abs):
                db_abs = os.path.join(site_path, "db", f"{project_name}.db")
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f"DATABASE_URL=\"file:{db_abs}\"",
                f"DB_FILE={db_abs}",
            ])

        # Site URL — используем domain_display (кириллицу) для URL
        if site_domain:
            proto = "https" if use_ssl else "http"
            domain_for_url = self.ctx.get("domain_display", site_domain)
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL={proto}://{domain_for_url}",
            ])
        else:
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL=http://localhost:{app_port}",
            ])

        # Security
        env_lines.extend([
            "",
            "# Security",
            f"SECRET_KEY={generate_password(32)}",
        ])

        # SSL
        env_lines.extend([
            "",
            "# SSL",
            f"ENABLE_SSL={'true' if use_ssl else 'false'}",
        ])

        # Добавляем пользовательские переменные из существующего .env
        known_keys = set()
        for line in env_lines:
            if '=' in line and not line.startswith('#'):
                known_keys.add(line.split('=', 1)[0].strip())

        custom_keys = {k: v for k, v in existing_env.items() if k not in known_keys}
        if custom_keys:
            env_lines.append("")
            env_lines.append("# User variables (preserved)")
            for k, v in custom_keys.items():
                env_lines.append(f"{k}={v}")

        env = '\n'.join(env_lines) + '\n'

        try:
            # Снимаем immutable-атрибут, если установлен (chattr +i от предыдущего деплоя)
            run_cmd(f"chattr -i {env_path} 2>/dev/null")
            os.makedirs(os.path.dirname(env_path), exist_ok=True)
            with open(env_path, 'w') as f:
                f.write(env)
            # Создаём .env.example — шаблон для репозитория
            # Этот файл БЕЗОПАСНО отслеживать в git — он содержит плейсхолдеры,
            # а не реальные значения. Нейронка видит .env.example и понимает
            # какие переменные нужны проекту.
            env_example_path = os.path.join(site_path, ".env.example")
            example_lines = [
                "# Application — скопируйте в .env и заполните реальными значениями",
                f"APP_NAME={project_name}",
                f"APP_PORT={app_port}",
                "NODE_ENV=production",
                "",
                "# Server",
                "HOST=0.0.0.0",
                f"PORT={app_port}",
                "",
                "# Database (SQLite)",
                '# ВАЖНО: На сервере используйте АБСОЛЮТНЫЙ путь!',
                '# Пример для сервера: DATABASE_URL="file:/var/www/example.ru/db/custom.db"',
                '# Пример локально: DATABASE_URL="file:./db/custom.db"',
                'DATABASE_URL="file:./db/custom.db"',
                "",
                "# Site",
                f"# NEXT_PUBLIC_SITE_URL=http://localhost:{app_port}",
                "",
                "# Security",
                "# SECRET_KEY=сгенерируется_автоматически",
                "",
                "# SSL",
                "ENABLE_SSL=false",
            ]
            try:
                with open(env_example_path, 'w') as f:
                    f.write('\n'.join(example_lines) + '\n')
            except Exception:
                pass  # .env.example — не критично

            return self._add_result(10, "env_file", "Создание .env", True,
                                    f"Создан: {env_path}",
                                    f"Шаблон: {env_example_path}")
        except Exception as e:
            return self._add_result(10, "env_file", "Создание .env", False, f"Ошибка: {e}")

    # ── Блок 11: Сборка и запуск ───────────────────────────────────────────

    def execute_block11(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        pm2_name = self.ctx.get("pm2_name", slugify_name(project_name))
        use_standalone = self.ctx.get("use_standalone", "no")
        steps = []
        has_error = False

        # 1. Создаём директории
        for d in ["logs", "public", "tmp"]:
            dp = os.path.join(site_path, d)
            os.makedirs(dp, exist_ok=True)
            steps.append(f"Создана: {d}/")

        # 2. Git init
        git_dir = os.path.join(site_path, ".git")
        if not os.path.exists(git_dir):
            rc, _, err = run_cmd(f"cd {site_path} && git init")
            steps.append("Git init" + ("" if rc == 0 else f": {err[:80]}"))
        else:
            steps.append("Git уже инициализирован")

        # Определяем текущую git-ветку и сохраняем в контекст
        rc_br, out_br, _ = run_cmd(f"cd {site_path} && git branch --show-current 2>/dev/null")
        current_branch = out_br.strip() if rc_br == 0 and out_br.strip() else "main"
        self.ctx["git_branch"] = current_branch
        steps.append(f"Git ветка: {current_branch}")

        # 3. npm install (С dev-зависимостями — нужно для next build)
        pkg = os.path.join(site_path, "package.json")
        if os.path.exists(pkg):
            node_modules = os.path.join(site_path, "node_modules")
            # Проверяем, не повреждён ли node_modules (битый SWC и т.п.)
            swc_file = os.path.join(node_modules, "@next", "swc-linux-x64-gnu",
                                    "next-swc.linux-x64-gnu.node")
            node_modules_ok = os.path.isdir(node_modules)
            if node_modules_ok and os.path.exists(swc_file):
                try:
                    swc_size = os.path.getsize(swc_file)
                    if swc_size < 1_000_000:  # < 1МБ — битый (должен быть ~130МБ)
                        console.print(f"  [yellow]SWC бинарник повреждён ({swc_size} байт), переустанавливаю...[/yellow]")
                        node_modules_ok = False
                except OSError:
                    node_modules_ok = False

            if not node_modules_ok:
                # Полная очистка и установка с нуля
                console.print("  [cyan]Очистка и установка зависимостей...[/cyan]")
                run_cmd(f"cd {site_path} && rm -rf node_modules package-lock.json")
                rc, out, err = run_cmd(
                    f"cd {site_path} && npm install --legacy-peer-deps", timeout=300
                )
                if rc == 0:
                    steps.append("npm install --legacy-peer-deps — OK")
                else:
                    steps.append(f"npm install — ошибка: {err[:200]}")
                    has_error = True
            else:
                steps.append("node_modules — уже установлены")

        # 4. next build (сборка проекта)
        if not has_error and os.path.exists(pkg):
            # Проверяем, нужна ли сборка (есть ли .next/standalone/server.js)
            standalone_path = os.path.join(site_path, ".next", "standalone", "server.js")
            next_dir = os.path.join(site_path, ".next")

            need_build = not os.path.isdir(next_dir)
            if not need_build and use_standalone == "yes" and not os.path.exists(standalone_path):
                need_build = True

            if need_build:
                # Убеждаемся, что db/ НЕ отслеживается git
                # (база данных — только серверные данные, не для репозитория)
                rc_db_track, out_db_track, _ = run_cmd(
                    f"cd {site_path} && git ls-files db/"
                )
                if out_db_track.strip():
                    run_cmd(f"cd {site_path} && git rm -r --cached db/ 2>/dev/null")
                    steps.append("db/ удалена из git (файлы остаются на диске)")

                # Убеждаемся, что .env НЕ отслеживается git
                rc_env_track, out_env_track, _ = run_cmd(
                    f"cd {site_path} && git ls-files .env .env.local .env.production"
                )
                if out_env_track.strip():
                    for env_f in out_env_track.strip().split('\n'):
                        env_f = env_f.strip()
                        if env_f:
                            run_cmd(f"cd {site_path} && git rm --cached {env_f} 2>/dev/null")
                    steps.append(".env удалён из git (файл остаётся на диске)")

                # Убеждаемся, что public/uploads/ НЕ отслеживается git
                # (пользовательские загрузки — только серверные данные)
                rc_uploads_track, out_uploads_track, _ = run_cmd(
                    f"cd {site_path} && git ls-files public/uploads/"
                )
                if out_uploads_track.strip():
                    # Бэкапим public/uploads/ перед удалением из git
                    uploads_dir = os.path.join(site_path, "public", "uploads")
                    uploads_backup = os.path.join(site_path, "tmp", "uploads_backup")
                    if os.path.isdir(uploads_dir):
                        try:
                            if os.path.isdir(uploads_backup):
                                shutil.rmtree(uploads_backup)
                            shutil.copytree(uploads_dir, uploads_backup)
                            steps.append("public/uploads/ → бэкап в tmp/uploads_backup/")
                        except Exception as e:
                            steps.append(f"Бэкап public/uploads/: {e}")
                    run_cmd(f"cd {site_path} && git rm -r --cached public/uploads/ 2>/dev/null")
                    steps.append("public/uploads/ удалена из git (файлы остаются на диске)")
                    # Восстанавливаем из бэкапа если git rm удалил файлы
                    if os.path.isdir(uploads_backup) and os.path.isdir(uploads_dir):
                        try:
                            # Копируем только файлы которых нет (не перезаписываем)
                            for root, dirs, files in os.walk(uploads_backup):
                                rel_root = os.path.relpath(root, uploads_backup)
                                dst_root = os.path.join(uploads_dir, rel_root)
                                os.makedirs(dst_root, exist_ok=True)
                                for f in files:
                                    src_f = os.path.join(root, f)
                                    dst_f = os.path.join(dst_root, f)
                                    if not os.path.exists(dst_f):
                                        shutil.copy2(src_f, dst_f)
                        except Exception:
                            pass

                # Создаём директорию db/ если нет (для SQLite базы)
                db_dir = os.path.join(site_path, "db")
                os.makedirs(db_dir, exist_ok=True)

                # Вычисляем env_prefix (DATABASE_URL) для Prisma и next build
                # ВАЖНО: вычисляем ДО prisma generate, чтобы переменная была доступна
                db_url = ""
                env_path_check = os.path.join(site_path, ".env")
                if os.path.exists(env_path_check):
                    try:
                        with open(env_path_check, 'r') as ef:
                            for line in ef:
                                line = line.strip()
                                if line.startswith('DATABASE_URL='):
                                    db_url = line.split('=', 1)[1].strip().strip('"').strip("'")
                                    break
                    except Exception:
                        pass

                env_prefix = f"DATABASE_URL='{db_url}' " if db_url else ""

                # prisma generate + migrate deploy — если есть prisma/schema.prisma
                prisma_schema = os.path.join(site_path, "prisma", "schema.prisma")
                if os.path.exists(prisma_schema):
                    console.print("  [cyan]Генерация Prisma клиента...[/cyan]")
                    # Передаём DATABASE_URL напрямую, а не через source .env
                    # source .env может падать на спецсимволах в паролях
                    rc_pg, _, err_pg = run_cmd(
                        f"cd {site_path} && {env_prefix}npx prisma generate 2>&1", timeout=120
                    )
                    if rc_pg == 0:
                        steps.append("prisma generate — OK")
                    else:
                        steps.append(f"prisma generate — ошибка (не критично): {err_pg[:100]}")

                    # prisma migrate deploy — БЕЗОПАСНО применяет ожидающие миграции
                    # ВАЖНО: используем migrate deploy, а НЕ migrate dev!
                    # migrate dev может пересоздать БД и удалить данные!
                    # migrate deploy только применяет новые миграции без потери данных
                    prisma_migrations = os.path.join(site_path, "prisma", "migrations")
                    if os.path.isdir(prisma_migrations):
                        console.print("  [cyan]Применение миграций Prisma...[/cyan]")
                        rc_pm, out_pm, err_pm = run_cmd(
                            f"cd {site_path} && {env_prefix}npx prisma migrate deploy 2>&1", timeout=120
                        )
                        if rc_pm == 0:
                            steps.append("prisma migrate deploy — OK")
                        else:
                            steps.append(f"prisma migrate deploy — ошибка: {err_pm[:150]}")

                console.print("  [cyan]Сборка проекта (next build)...[/cyan]")
                # env_prefix уже вычислен выше (перед prisma generate)
                build_cmd = (f"cd {site_path} && "
                             f"{env_prefix}"
                             f"NODE_OPTIONS='--max-old-space-size=4096' npm run build")
                rc, out, err = run_cmd(build_cmd, timeout=600)
                if rc == 0:
                    steps.append("next build — OK")
                else:
                    # Проверяем, может быть Bus error (OOM) — пробуем с меньшим лимитом
                    err_text = (err or out or "")[-500:]
                    if 'Bus error' in err_text or 'core dumped' in err_text or rc == -1:
                        console.print("  [yellow]Build упал (возможно OOM), пробую с 2ГБ лимитом...[/yellow]")
                        build_cmd2 = (f"cd {site_path} && "
                                      f"{env_prefix}"
                                      f"NODE_OPTIONS='--max-old-space-size=2048' npm run build")
                        rc2, out2, err2 = run_cmd(build_cmd2, timeout=600)
                        if rc2 == 0:
                            steps.append("next build (2ГБ лимит) — OK")
                            rc, err = rc2, err2
                        else:
                            steps.append("next build — ошибка (не хватает памяти?)")
                            steps.append(f"  {(err2 or err or '')[-300:]}")
                            has_error = True
                    else:
                        steps.append("next build — ошибка")
                        steps.append(f"  {err_text[-300:]}")
                        has_error = True
            else:
                steps.append(".next — уже собран, сборка не нужна")

            # Проверяем standalone после сборки
            # Даже если build вернул ошибку, standalone мог создаться
            # (npm run build = next build && cp static && cp public)
            # Если next build прошёл, но cp упал — standalone рабочий!
            if use_standalone == "yes" and os.path.exists(standalone_path):
                if has_error:
                    # Build вернул ошибку, но standalone есть — пробуем запустить
                    console.print("  [yellow]Build вернул ошибку, но standalone создан — пробую запустить...[/yellow]")
                    has_error = False  # Сбрасываем, чтобы PM2 запустился

            if not has_error and use_standalone == "yes":
                if os.path.exists(standalone_path):
                    steps.append("standalone server.js — создан")

                    # Копируем .env в standalone-директорию
                    # Standalone запускается из .next/standalone/ и читает
                    # .env оттуда, а не из корня проекта
                    standalone_dir = os.path.join(site_path, ".next", "standalone")
                    src_env = os.path.join(site_path, ".env")
                    dst_env = os.path.join(standalone_dir, ".env")
                    if os.path.exists(src_env):
                        try:
                            shutil.copy2(src_env, dst_env)
                            steps.append(".env скопирован в .next/standalone/")
                        except Exception as e:
                            steps.append(f"Копирование .env в standalone: {e}")

                    # Копируем public/ в standalone (для статики)
                    src_public = os.path.join(site_path, "public")
                    dst_public = os.path.join(standalone_dir, "public")
                    if os.path.isdir(src_public) and not os.path.isdir(dst_public):
                        try:
                            shutil.copytree(src_public, dst_public)
                        except Exception:
                            pass

                    # Копируем статику .next/static в standalone/.next/static
                    # (Nginx может раздавать из корневого .next/static,
                    #  но standalone тоже должен иметь свою копию)
                    src_static = os.path.join(site_path, ".next", "static")
                    dst_static = os.path.join(standalone_dir, ".next", "static")
                    if os.path.isdir(src_static) and not os.path.isdir(dst_static):
                        try:
                            shutil.copytree(src_static, dst_static)
                        except Exception:
                            pass

                    # Копируем db/ в standalone (SQLite база данных)
                    # ВАЖНО: НЕ перезаписываем существующие .db файлы!
                    # На сервере база содержит живые данные (загрузки, сессии и т.д.)
                    # Если перезаписать — данные потеряются
                    src_db = os.path.join(site_path, "db")
                    dst_db = os.path.join(standalone_dir, "db")
                    if os.path.isdir(src_db):
                        try:
                            os.makedirs(dst_db, exist_ok=True)
                            for item in os.listdir(src_db):
                                src_item = os.path.join(src_db, item)
                                dst_item = os.path.join(dst_db, item)
                                if os.path.isfile(src_item):
                                    if not os.path.exists(dst_item):
                                        shutil.copy2(src_item, dst_item)
                                        steps.append(f"db/{item} скопирован в standalone")
                                    # Если файл уже есть — НЕ перезаписываем (сохраняем данные)
                                elif os.path.isdir(src_item):
                                    if not os.path.isdir(dst_item):
                                        shutil.copytree(src_item, dst_item)
                        except Exception as e:
                            steps.append(f"Копирование db/ в standalone: {e}")

                    # Копируем prisma/ в standalone (схема для prisma generate)
                    src_prisma = os.path.join(site_path, "prisma")
                    dst_prisma = os.path.join(standalone_dir, "prisma")
                    if os.path.isdir(src_prisma):
                        try:
                            if os.path.isdir(dst_prisma):
                                shutil.rmtree(dst_prisma)
                            shutil.copytree(src_prisma, dst_prisma)
                            steps.append("prisma/ скопирован в .next/standalone/")
                        except Exception as e:
                            steps.append(f"Копирование prisma/ в standalone: {e}")
                else:
                    steps.append("standalone server.js — НЕ НАЙДЕН, переключаюсь на Standard")
                    self.ctx["use_standalone"] = "no"
                    self.ctx["pm2_mode_label"] = "Standard"
                    self.ctx["pm2_script"] = "npx"
                    app_port = self.ctx.get("app_port", 3000)
                    self.ctx["pm2_args"] = f"next start -p {app_port}"
                    # Пересоздаём ecosystem.config.js для Standard режима
                    # Используем npx next start -p PORT вместо npm start,
                    # т.к. npm start может хардкодить порт в package.json
                    eco_content = f"""module.exports = {{
  apps: [{{
    name: '{pm2_name}',
    script: 'npx',
    args: 'next start -p {app_port}',
    cwd: '{site_path}',
    env: {{
      PORT: {app_port},
      NODE_ENV: 'production',
      HOSTNAME: '0.0.0.0'
    }},
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    error_file: '{site_path}/logs/pm2-error.log',
    out_file: '{site_path}/logs/pm2-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }}]
}};
"""
                    eco_path = os.path.join(site_path, "ecosystem.config.js")
                    # Снимаем immutable-атрибут, если установлен
                    run_cmd(f"chattr -i {eco_path} 2>/dev/null")
                    with open(eco_path, 'w') as f:
                        f.write(eco_content)
                    steps.append("ecosystem.config.js пересоздан для Standard")

        # 5. Запуск PM2 (после сборки!)
        if not has_error:
            # Удаляем старый процесс если есть (и сайт, и webhook)
            run_cmd(f"pm2 delete {pm2_name} 2>/dev/null")
            run_cmd(f"pm2 delete {pm2_name}-webhook 2>/dev/null")

            # Проверяем: нет ли PM2-процесса с тем же именем из другой директории?
            # (случается когда домен содержит точку: git.be1st.pro → slug git-be1st-pro,
            #  но ранее был создан сайт /var/www/git.be1st.pro с тем же PM2-слагом)
            try:
                rc_check, out_check, _ = run_cmd(f"pm2 describe {pm2_name} 2>/dev/null")
                if rc_check == 0 and out_check:
                    import re as _re
                    _cwd_match = _re.search(r'exec cwd\s+│\s+(\S+)', out_check)
                    if _cwd_match:
                        _existing_cwd = _cwd_match.group(1).rstrip('/')
                        _expected_cwd = site_path.rstrip('/')
                        if _existing_cwd != _expected_cwd:
                            console.print(
                                f"  [yellow]⚠ PM2 конфликт: процесс '{pm2_name}' "
                                f"указывает на {_existing_cwd}, а нужно {_expected_cwd}[/yellow]"
                            )
                            console.print(
                                f"  [cyan]Удаляю конфликтующие процессы...[/cyan]"
                            )
                            run_cmd(f"pm2 delete {pm2_name} 2>/dev/null")
                            run_cmd(f"pm2 delete {pm2_name}-webhook 2>/dev/null")
                            run_cmd("pm2 save 2>/dev/null")
                            console.print(
                                f"  [green]Конфликтующие процессы удалены[/green]"
                            )
            except Exception:
                pass

            # HOSTNAME=0.0.0.0 — обязательно! Иначе Next.js standalone
            # слушает только localhost и возвращает 502 через nginx
            rc, out, err = run_cmd(
                f"cd {site_path} && HOSTNAME=0.0.0.0 HOST=0.0.0.0 "
                f"pm2 start ecosystem.config.js --update-env"
            )
            if rc == 0:
                run_cmd("pm2 save")
                mode_label = self.ctx.get("pm2_mode_label", "Standard")
                steps.append(f"PM2 запущен ({mode_label})")
            else:
                steps.append(f"PM2 — ошибка: {err[:150]}")
                has_error = True

            # Защита .env и ecosystem.config.js от случайного удаления/перезаписи
            # chattr +i делает файл неизменяемым (даже для root, пока не сделаешь chattr -i)
            env_path = os.path.join(site_path, ".env")
            eco_path = os.path.join(site_path, "ecosystem.config.js")
            for protected_file in [env_path, eco_path]:
                if os.path.exists(protected_file):
                    run_cmd(f"chattr +i {protected_file} 2>/dev/null")
            steps.append("chattr +i защита установлена (.env, ecosystem.config.js)")

        msg = "Завершена" if not has_error else "Завершена с ошибками"
        return self._add_result(11, "auto_setup", "Сборка и запуск", not has_error,
                                msg, '\n'.join(steps))

    # ── Блок 12: Проверка репо и токена (check) ──────────────────────────

    def execute_block12(self) -> BlockResult:
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")
        github_token_retry = self.ctx.get("github_token_retry", "")
        site_path = self.ctx.get("site_path", ".")

        if github_token_retry:
            github_token = github_token_retry
            self.ctx["github_token"] = github_token
            self.ctx["github_token_retry"] = ""

        if not repo_name:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "Имя репозитория не задано")
        if not github_token:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "GitHub Token не указан")

        rc, out, err = run_cmd(
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"-H 'Authorization: token {github_token}' "
            f"https://api.github.com/repos/{repo_name}"
        )

        if rc == 0:
            status = out.strip().replace("'", "")
            if status == "200":
                run_cmd(f"cd {site_path} && git remote get-url origin 2>/dev/null")
                run_cmd(
                    f"cd {site_path} && git remote set-url origin "
                    f"https://{github_token}@github.com/{repo_name}.git 2>/dev/null || "
                    f"git remote add origin https://{github_token}@github.com/{repo_name}.git"
                )
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", True,
                                        f"Репозиторий {repo_name} доступен")
            elif status == "404":
                # Репозиторий не найден — пользователь должен создать вручную
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        f"Репозиторий {repo_name} не найден на GitHub",
                                        "Создайте репозиторий вручную на github.com, затем повторите проверку")
            elif status == "401":
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        "Токен GitHub неверен или истёк",
                                        "Нужен новый токен с правами repo")
            else:
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        f"Неожиданный ответ: {status}", err[:200])
        else:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "Не удалось подключиться к GitHub API", err[:200])

    # ── Блок 13: Первый коммит (авто) ────────────────────────────────────

    def execute_block13(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")
        git_branch = self.ctx.get("git_branch", "main")

        # Убеждаемся, что .env и другие серверные файлы НЕ отслеживаются git
        # (git rm --cached удаляет из индекса, но оставляет файл на диске)
        # ВАЖНО: нейронка может случайно закоммитить .env с путями от нейронки
        # (например /home/z/my-project/), что при git reset --hard перезапишет
        # серверный .env. Поэтому всегда убираем эти файлы из отслеживания.
        _untrack_files = [
            ".env", ".env.local", ".env.production", ".env.webhook",
            "ecosystem.config.js", "Caddyfile", "worklog.md",
        ]
        _untrack_dirs = ["db/", "upload/", "tool-results/", "logs/", "tmp/"]
        
        # Проверяем и убираем отдельные файлы
        for _uf in _untrack_files:
            rc_t, out_t, _ = run_cmd(f"cd {site_path} && git ls-files {_uf}")
            if out_t.strip():
                run_cmd(f"cd {site_path} && git rm --cached {_uf} 2>/dev/null")
                console.print(f"  [yellow]Удалён из git: {_uf} (файл остаётся на диске)[/yellow]")
        
        # Проверяем и убираем директории
        for _ud in _untrack_dirs:
            rc_t, out_t, _ = run_cmd(f"cd {site_path} && git ls-files {_ud}")
            if out_t.strip():
                run_cmd(f"cd {site_path} && git rm -r --cached {_ud} 2>/dev/null")
                console.print(f"  [yellow]Удалена из git: {_ud}/ (файлы остаются на диске)[/yellow]")
        
        # Убежимся, что .gitignore содержит все нужные записи
        _gitignore_path = os.path.join(site_path, ".gitignore")
        _required_gitignore = [
            ".env*", "db/", "ecosystem.config.js", "upload/",
            "tool-results/", "Caddyfile", "worklog.md", "logs/", "tmp/",
        ]
        if os.path.exists(_gitignore_path):
            try:
                with open(_gitignore_path, "r") as f:
                    _gi_content = f.read()
                _gi_modified = False
                for _entry in _required_gitignore:
                    if _entry not in _gi_content:
                        _gi_content += f"\n{_entry}"
                        _gi_modified = True
                if _gi_modified:
                    with open(_gitignore_path, "w") as f:
                        f.write(_gi_content)
                    console.print("  [cyan].gitinfo updated with missing entries[/cyan]")
            except Exception:
                pass
        
        # Создаём .env.server-backup для восстановления при будущих деплоях
        _env_path = os.path.join(site_path, ".env")
        _backup_path = os.path.join(site_path, ".env.server-backup")
        if os.path.exists(_env_path):
            try:
                import shutil
                shutil.copy2(_env_path, _backup_path)
            except Exception:
                pass

        rc, _, err = run_cmd(f"cd {site_path} && git add -A")
        if rc != 0:
            return self._add_result(13, "first_commit", "Первый коммит и пуш", False,
                                    "Ошибка git add", err[:200])

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        commit_msg = f"AutoDeploy: initial commit for {project_name} [{timestamp}]"

        rc, out, err = run_cmd(f'cd {site_path} && git commit -m "{commit_msg}"')
        if rc != 0 and "nothing to commit" not in out:
            return self._add_result(13, "first_commit", "Первый коммит и пуш", False,
                                    "Ошибка git commit", f"{out}\n{err}"[:300])

        if repo_name and github_token:
            # Устанавливаем remote URL с токеном
            run_cmd(
                f"cd {site_path} && git remote set-url origin "
                f"https://{github_token}@github.com/{repo_name}.git"
            )

            # Убежимся, что локальная ветка совпадает с git_branch
            rc_branch, out_branch, _ = run_cmd(
                f"cd {site_path} && git branch --show-current"
            )
            current_branch = out_branch.strip() or git_branch
            if current_branch != git_branch:
                run_cmd(f"cd {site_path} && git branch -m {current_branch} {git_branch}")

            # Подтягиваем изменения с remote (если remote не пустой)
            # ВАЖНО: при rebase стратегия -X theirs означает
            # «предпочесть НАШИ локальные изменения» (т.к. при rebase
            # ours = upstream/remote, theirs = локальные коммиты)
            run_cmd(
                f"cd {site_path} && git pull origin {git_branch} "
                f"--rebase --allow-unrelated-histories -X theirs 2>&1"
            )
            # Ошибки pull не критичны (remote может быть пустым или без ветки)

            # Восстанавливаем .env после pull — git мог затереть наш файл
            self._regenerate_env_after_pull(site_path)

            # Пробуем обычный push
            rc, out, err = run_cmd(
                f"cd {site_path} && git push -u origin {git_branch} 2>&1"
            )
            if rc != 0 and "Everything up-to-date" not in out:
                # Если push отклонён (non-fast-forward) — force push
                # Сервер — источник истины при деплое
                if "non-fast-forward" in (out + err) or "behind" in (out + err):
                    console.print(
                        "  [yellow]Remote имеет другие коммиты, "
                        "выполняю force push (сервер — источник истины)...[/yellow]"
                    )
                    rc, out, err = run_cmd(
                        f"cd {site_path} && git push -u origin {git_branch} --force 2>&1"
                    )
                    if rc != 0:
                        return self._add_result(
                            13, "first_commit", "Первый коммит и пуш", False,
                            "Ошибка git push --force",
                            f"{out}\n{err}"[:300]
                        )
                    return self._add_result(
                        13, "first_commit", "Первый коммит и пуш", True,
                        f"Коммит: '{commit_msg}'",
                        "Force push выполнен (сервер — источник истины)"
                    )
                return self._add_result(
                    13, "first_commit", "Первый коммит и пуш", False,
                    "Ошибка git push", f"{out}\n{err}"[:300]
                )
            return self._add_result(
                13, "first_commit", "Первый коммит и пуш", True,
                f"Коммит: '{commit_msg}'", "Push выполнен"
            )
        else:
            return self._add_result(
                13, "first_commit", "Первый коммит и пуш", True,
                f"Коммит: '{commit_msg}'",
                "Push пропущен (нет репо/токена)"
            )

    def _regenerate_env_after_pull(self, site_path: str):
        """Пересоздаёт .env после git pull — git мог затереть наш .env.
        
        Это страховка: даже если .env попал в remote repo (ошибка),
        мы восстанавливаем правильные значения из контекста деплоя.
        """
        env_path = os.path.join(site_path, ".env")
        app_port = self.ctx.get("app_port", 3000)
        db_file = self.ctx.get("db_file", "")
        site_domain = self.ctx.get("site_domain", "")
        use_ssl = self.ctx.get("use_ssl", False)
        project_name = self.ctx.get("project_name", "app")

        # Сначала читаем существующий .env — нужно сохранить SECRET_KEY
        existing_env = {}
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            existing_env[k.strip()] = v.strip()
            except Exception:
                pass

        env_lines = [
            "# Application",
            f"APP_NAME={project_name}",
            f"APP_PORT={app_port}",
            f"NODE_ENV=production",
            "",
            "# Server",
            f"HOST=0.0.0.0",
            f"PORT={app_port}",
        ]

        if db_file:
            # Убеждаемся, что путь абсолютный
            if not os.path.isabs(db_file):
                db_file = os.path.abspath(os.path.join(site_path, db_file))
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f'DATABASE_URL="file:{db_file}"',
                f"DB_FILE={db_file}",
            ])
        else:
            # Пробуем найти db/custom.db (стандартный путь в шаблонах)
            # Создаём директорию db/ если нет — DATABASE_URL должен указывать
            # на существующую директорию, иначе Prisma упадёт при migrate
            db_dir = os.path.join(site_path, "db")
            os.makedirs(db_dir, exist_ok=True)

            db_abs = os.path.join(site_path, "db", "custom.db")
            if not os.path.exists(db_abs):
                db_abs = os.path.join(site_path, "db", f"{project_name}.db")
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f'DATABASE_URL="file:{db_abs}"',
                f"DB_FILE={db_abs}",
            ])

        if site_domain:
            proto = "https" if use_ssl else "http"
            domain_for_url = self.ctx.get("domain_display", site_domain)
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL={proto}://{domain_for_url}",
            ])
        else:
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL=http://localhost:{app_port}",
            ])

        # Сохраняем существующий SECRET_KEY чтобы не инвалидировать сессии
        existing_secret = existing_env.get("SECRET_KEY", "")

        env_lines.extend([
            "",
            "# Security",
            f"SECRET_KEY={existing_secret or generate_password(32)}",
            "",
            "# SSL",
            f"ENABLE_SSL={'true' if use_ssl else 'false'}",
        ])

        # Сохраняем пользовательские переменные из текущего .env
        # (existing_env уже прочитан выше)

        known_keys = set()
        for line in env_lines:
            if '=' in line and not line.startswith('#'):
                known_keys.add(line.split('=', 1)[0].strip())

        custom_keys = {k: v for k, v in existing_env.items() if k not in known_keys}
        if custom_keys:
            env_lines.append("")
            env_lines.append("# User variables (preserved)")
            for k, v in custom_keys.items():
                env_lines.append(f"{k}={v}")

        try:
            # Снимаем immutable-атрибут, если установлен
            run_cmd(f"chattr -i {env_path} 2>/dev/null")
            with open(env_path, 'w') as f:
                f.write('\n'.join(env_lines) + '\n')
            console.print("  [green].env восстановлен после git pull[/green]")
        except Exception:
            console.print("  [red]Не удалось восстановить .env после git pull[/red]")

        # Также обновляем .env в standalone-директории
        standalone_dir = os.path.join(site_path, ".next", "standalone")
        dst_env = os.path.join(standalone_dir, ".env")
        if os.path.isdir(standalone_dir):
            try:
                shutil.copy2(env_path, dst_env)
            except Exception:
                pass

    # ── Блок 14: Финальные проверки (авто) ───────────────────────────────

    def execute_block14(self) -> BlockResult:
        checks = []
        all_ok = True
        project_name = self.ctx.get("project_name", "app")
        pm2_name = self.ctx.get("pm2_name", slugify_name(project_name))
        site_path = self.ctx.get("site_path", ".")

        rc, _, _ = run_cmd(f"pm2 describe {pm2_name} 2>/dev/null")
        if rc == 0:
            checks.append("PM2 процесс — запущен")
        else:
            checks.append("PM2 процесс — НЕ НАЙДЕН")
            all_ok = False

        if os.path.exists(os.path.join(site_path, ".env")):
            checks.append(".env — существует")
        else:
            checks.append(".env — НЕ НАЙДЕН")
            all_ok = False

        if os.path.exists(os.path.join(site_path, ".gitignore")):
            checks.append(".gitignore — существует")
        else:
            checks.append(".gitignore — НЕ НАЙДЕН")
            all_ok = False

        rc, out, _ = run_cmd(f"cd {site_path} && git remote -v")
        checks.append("Git remote — " + ("настроен" if "origin" in out else "НЕ НАСТРОЕН"))

        app_port = self.ctx.get("app_port", 0)
        checks.append(f"Порт: {app_port}" if app_port else "Порт — НЕ ВЫБРАН")

        # Проверяем DNS-резолвинг домена
        domain = self.ctx.get("site_domain", "") or self.ctx.get("domain", "")
        if domain:
            rc, out, _ = run_cmd(f"dig +short {domain} A 2>/dev/null | head -1")
            dns_ip = out.strip()
            if dns_ip:
                checks.append(f"DNS {domain} → {dns_ip}")
            else:
                checks.append(f"DNS {domain} — НЕ РЕЗОЛВИТСЯ! Настройте A-запись на IP сервера")
                all_ok = False

        # Проверяем HTTP-доступ через веб-сервер
        if domain:
            rc, out, _ = run_cmd(
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"-H 'Host: {domain}' http://127.0.0.1:80 --max-time 5"
            )
            status = out.strip().replace("'", "")
            if status in ("200", "301", "302", "304"):
                checks.append(f"HTTP через веб-сервер — OK (статус {status})")
            elif status == "000":
                checks.append("HTTP через веб-сервер — НЕТ ОТВЕТА")
                all_ok = False
            else:
                checks.append(f"HTTP через веб-сервер — статус {status}")
                if status in ("502", "503"):
                    all_ok = False

        # Проверяем прямое подключение к приложению
        if app_port:
            rc, out, _ = run_cmd(
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"http://127.0.0.1:{app_port}/ --max-time 3"
            )
            direct_status = out.strip().replace("'", "")
            if direct_status in ("200", "301", "302", "304"):
                checks.append(f"Прямой HTTP на порт {app_port} — OK")
            else:
                checks.append(f"Прямой HTTP на порт {app_port} — статус {direct_status}")
                all_ok = False

        msg = "Все проверки пройдены" if all_ok else "Есть проблемы"
        return self._add_result(14, "final_checks", "Финальные проверки", all_ok,
                                msg, '\n'.join(checks))

    # ── Блок 15: Автодеплой (авто) ───────────────────────────────────────

    def execute_block15(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        pm2_name = self.ctx.get("pm2_name", slugify_name(project_name))
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")

        webhook_port = find_free_port(9000, 49000, self.used_ports)
        self.used_ports.add(webhook_port)

        # Проверяем, есть ли уже секрет от предыдущего деплоя
        webhook_env_path = os.path.join(site_path, ".env.webhook")
        existing_secret = ""
        existing_webhook_port = None
        if os.path.exists(webhook_env_path):
            try:
                with open(webhook_env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("WEBHOOK_SECRET="):
                            existing_secret = line.split("=", 1)[1].strip()
                        elif line.startswith("WEBHOOK_PORT="):
                            try:
                                existing_webhook_port = int(line.split("=", 1)[1].strip())
                            except ValueError:
                                pass
            except Exception:
                pass

        # Переиспользуем секрет и порт если они были
        if existing_secret:
            secret = existing_secret
            console.print(f"  [cyan]Переиспользуем существующий webhook secret[/cyan]")
        else:
            # Webhook secret: только буквенно-цифровые символы и -, _
            # Спецсимволы (!@#$%^&*) могут ломать JS-строку в webhook-server.js
            secret = ''.join(random.choice(string.ascii_letters + string.digits + '-_') for _ in range(32))
        if existing_webhook_port and existing_webhook_port not in self.used_ports:
            # Проверяем, что порт реально свободен в системе
            # (может быть занят другим сайтом, если .env.webhook устарел)
            port_is_free = True
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.bind(('0.0.0.0', existing_webhook_port))
                s.close()
            except (OSError, socket.error):
                port_is_free = False

            if port_is_free:
                webhook_port = existing_webhook_port
                self.used_ports.add(webhook_port)
                console.print(f"  [cyan]Переиспользуем существующий webhook порт: {webhook_port}[/cyan]")
            else:
                console.print(f"  [yellow]Существующий webhook порт {existing_webhook_port} занят, назначается новый[/yellow]")
                self.used_ports.add(webhook_port)  # webhook_port уже назначен через find_free_port выше

        # Сохраняем секрет и порт в .env.webhook
        try:
            with open(webhook_env_path, 'w') as f:
                f.write(f"WEBHOOK_SECRET={secret}\n")
                f.write(f"WEBHOOK_PORT={webhook_port}\n")
                f.write(f"WEBHOOK_PM2_NAME={pm2_name}-webhook\n")
        except Exception:
            pass

        git_branch = self.ctx.get("git_branch", "main")
        run_mode = self.ctx.get("run_mode", "standalone")
        site_port = self.ctx.get("app_port", "3000")

        webhook_script = f"""#!/usr/bin/env node
const http = require('http');
const crypto = require('crypto');
const {{ exec }} = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = {webhook_port};
const SECRET = '{secret}';
const PROJECT = '{pm2_name}';
const SITE_PATH = '{site_path}';
const GIT_BRANCH = '{git_branch}';
const RUN_MODE = '{run_mode}';
const SITE_PORT = '{site_port}';
const ENV_FILE = path.join(SITE_PATH, '.env');

// Проверяем подпись GitHub (HMAC-SHA256)
function verifySignature(body, signature) {{
  if (!signature) {{
    console.warn('[webhook] No signature header — request rejected');
    return false;
  }}
  const expected = 'sha256=' + crypto.createHmac('sha256', SECRET)
    .update(body).digest('hex');
  try {{
    const a = Buffer.from(signature);
    const b = Buffer.from(expected);
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }} catch (e) {{
    return false;
  }}
}}

// Сохраняем .env перед git fetch чтобы не потерять настройки
function backupEnv() {{
  try {{
    if (fs.existsSync(ENV_FILE)) {{
      return fs.readFileSync(ENV_FILE, 'utf8');
    }}
  }} catch (e) {{}}
  return null;
}}

// Восстанавливаем .env после git reset если он был изменён
function restoreEnv(originalContent) {{
  try {{
    if (originalContent && fs.existsSync(ENV_FILE)) {{
      const current = fs.readFileSync(ENV_FILE, 'utf8');
      if (current !== originalContent) {{
        fs.writeFileSync(ENV_FILE, originalContent, 'utf8');
        console.log('[webhook] .env restored after git reset (was overwritten)');
      }}
    }}
  }} catch (e) {{
    console.error('[webhook] Failed to restore .env:', e.message);
  }}
}}

// Копируем .env, static, db, prisma в standalone-директорию
function copyToStandalone() {{
  try {{
    const standaloneDir = path.join(SITE_PATH, '.next', 'standalone');
    if (!fs.existsSync(standaloneDir)) {{
      console.log('[webhook] No standalone dir, skipping copy');
      return;
    }}
    // Копируем .env
    if (fs.existsSync(ENV_FILE)) {{
      fs.copyFileSync(ENV_FILE, path.join(standaloneDir, '.env'));
      console.log('[webhook] .env copied to .next/standalone/');
    }}
    // Копируем static файлы (CSS, JS, шрифты)
    const staticSrc = path.join(SITE_PATH, '.next', 'static');
    const staticDst = path.join(standaloneDir, '.next', 'static');
    if (fs.existsSync(staticSrc) && !fs.existsSync(staticDst)) {{
      fs.cpSync(staticSrc, staticDst, {{ recursive: true }});
      console.log('[webhook] .next/static copied to standalone');
    }}
    // Копируем public — без перезаписи существующих файлов
    // (пользовательские загрузки в standalone/public/uploads/ не трогаем)
    const publicSrc = path.join(SITE_PATH, 'public');
    const publicDst = path.join(standaloneDir, 'public');
    if (fs.existsSync(publicSrc)) {{
      const copyDirNoOverwrite = (src, dst) => {{
        if (!fs.existsSync(dst)) fs.mkdirSync(dst, {{ recursive: true }});
        for (const entry of fs.readdirSync(src)) {{
          const srcPath = path.join(src, entry);
          const dstPath = path.join(dst, entry);
          if (fs.statSync(srcPath).isDirectory()) {{
            copyDirNoOverwrite(srcPath, dstPath);
          }} else {{
            if (!fs.existsSync(dstPath)) {{
              fs.copyFileSync(srcPath, dstPath);
            }}
          }}
        }}
      }};
      copyDirNoOverwrite(publicSrc, publicDst);
      console.log('[webhook] public copied to standalone (no overwrite)');
    }}
    // Копируем db/ (SQLite база данных)
    // ВАЖНО: НЕ перезаписываем существующие .db файлы!
    // На сервере база содержит живые данные (загрузки, сессии и т.д.)
    const dbSrc = path.join(SITE_PATH, 'db');
    const dbDst = path.join(standaloneDir, 'db');
    if (fs.existsSync(dbSrc)) {{
      if (!fs.existsSync(dbDst)) {{
        fs.mkdirSync(dbDst, {{ recursive: true }});
      }}
      // Копируем только файлы, которых ещё нет в standalone/db/
      // (не перезаписываем серверную базу данных!)
      const dbFiles = fs.readdirSync(dbSrc);
      for (const file of dbFiles) {{
        const srcFile = path.join(dbSrc, file);
        const dstFile = path.join(dbDst, file);
        if (!fs.existsSync(dstFile)) {{
          if (fs.statSync(srcFile).isDirectory()) {{
            fs.cpSync(srcFile, dstFile, {{ recursive: true }});
          }} else {{
            fs.copyFileSync(srcFile, dstFile);
          }}
          console.log(`[webhook] db/${{file}} copied to standalone`);
        }} else {{
          console.log(`[webhook] db/${{file}} already exists in standalone, skipping (preserving server data)`);
        }}
      }}
    }}
    // Копируем prisma/ (схема нужна для prisma generate)
    const prismaSrc = path.join(SITE_PATH, 'prisma');
    const prismaDst = path.join(standaloneDir, 'prisma');
    if (fs.existsSync(prismaSrc)) {{
      if (!fs.existsSync(prismaDst)) {{
        fs.mkdirSync(prismaDst, {{ recursive: true }});
      }}
      fs.cpSync(prismaSrc, prismaDst, {{ recursive: true }});
      console.log('[webhook] prisma/ copied to standalone');
    }}
  }} catch (e) {{
    console.error('[webhook] Failed to copy to standalone:', e.message);
  }}
}}

// Безопасное выполнение команды с логированием
function runCmd(cmd, label, callback) {{
  console.log(`[webhook] Running: ${{label}}`);
  exec(cmd, {{ maxBuffer: 10 * 1024 * 1024 }}, (err, stdout, stderr) => {{
    if (err) {{
      console.error(`[webhook] ${{label}} ERROR:`, err.message);
      if (stderr) console.error(`[webhook] ${{label}} stderr:`, stderr.slice(0, 500));
    }} else {{
      console.log(`[webhook] ${{label}} OK`);
      if (stdout && stdout.length < 500) console.log(`[webhook] ${{label}} output:`, stdout.trim());
    }}
    callback(err);
  }});
}}

const server = http.createServer((req, res) => {{
  if (req.method === 'POST' && req.url === '/webhook') {{
    let body = '';
    req.on('error', (e) => {{
      console.error('[webhook] Request error:', e.message);
    }});
    req.on('data', chunk => {{ body += chunk; }});
    req.on('end', () => {{
      try {{
        // Проверяем подпись GitHub
        const signature = req.headers['x-hub-signature-256'];
        if (!verifySignature(body, signature)) {{
          res.writeHead(403, {{'Content-Type': 'application/json'}});
          res.end(JSON.stringify({{ status: 'error', message: 'Invalid signature' }}));
          return;
        }}

        const payload = JSON.parse(body);
        const ref = payload.ref || '';
        const branch = ref.replace('refs/heads/', '');
        console.log(`[${{new Date().toISOString()}}] Webhook received for ${{payload.repository?.name || 'unknown'}} branch=${{branch}}`);

        // Игнорируем push в другие ветки
        if (branch && branch !== GIT_BRANCH) {{
          console.log(`[webhook] Ignoring push to ${{branch}} (watching ${{GIT_BRANCH}})`);
          res.writeHead(200, {{'Content-Type': 'application/json'}});
          res.end(JSON.stringify({{ status: 'ignored', message: `Branch ${{branch}} not watched` }}));
          return;
        }}

        // 1. Бэкапим .env
        const envBackup = backupEnv();

        // 2. Запоминаем текущий коммит для отката при ошибке сборки
        runCmd(`cd ${{SITE_PATH}} && git rev-parse HEAD`, 'get current commit', (revErr, revStdout) => {{
          const prevCommit = revStdout ? revStdout.trim() : '';

          // 2a. Бэкапим public/uploads/ ДО git reset — чтобы не потерять
          // пользовательские загрузки (логотип, favicon, файлы из админки)
          let uploadsBackup = null;
          const uploadsDir = path.join(SITE_PATH, 'public', 'uploads');
          if (fs.existsSync(uploadsDir)) {{
            try {{
              uploadsBackup = path.join(SITE_PATH, 'tmp', 'uploads_webhook_backup');
              if (fs.existsSync(uploadsBackup)) fs.rmSync(uploadsBackup, {{ recursive: true }});
              fs.cpSync(uploadsDir, uploadsBackup, {{ recursive: true }});
              console.log('[webhook] public/uploads/ backed up before git reset');
            }} catch (e) {{
              console.error('[webhook] Failed to backup uploads:', e.message);
              uploadsBackup = null;
            }}
          }}

          // 3. Снимаем immutable-атрибут (chattr +i) перед git reset
          // autodeploy.py устанавливает chattr +i на .env и ecosystem.config.js
          // чтобы защитить их от перезаписи при git reset --hard
          // chattr -R -i + git fetch + git reset — одной командой
          runCmd(`cd ${{SITE_PATH}} && chattr -R -i ${{SITE_PATH}}/ 2>/dev/null; git fetch origin ${{GIT_BRANCH}} 2>&1 && git reset --hard origin/${{GIT_BRANCH}} 2>&1`, 'chattr -R -i + git fetch + reset', (pullErr) => {{
            // 3a. Убираем db/, .env, public/uploads/ из git tracking
            // Это страховка: даже если нейронка случайно закоммитила .env, db/ или uploads/,
            // мы убираем их из отслеживания (файлы остаются на диске)
            runCmd(`cd ${{SITE_PATH}} && git rm -r --cached db/ 2>/dev/null; git rm --cached .env .env.local .env.production 2>/dev/null; git rm -r --cached public/uploads/ 2>/dev/null; true`, 'ensure db/, .env, uploads untracked', () => {{}});

            // 3b. Восстанавливаем public/uploads/ из бэкапа
            // git reset --hard мог удалить пользовательские загрузки
            if (uploadsBackup && fs.existsSync(uploadsBackup)) {{
              try {{
                // Копируем файлы которых нет в public/uploads/ (не перезаписываем новые)
                const restoreDir = (src, dst) => {{
                  if (!fs.existsSync(dst)) fs.mkdirSync(dst, {{ recursive: true }});
                  for (const entry of fs.readdirSync(src)) {{
                    const srcPath = path.join(src, entry);
                    const dstPath = path.join(dst, entry);
                    if (fs.statSync(srcPath).isDirectory()) {{
                      restoreDir(srcPath, dstPath);
                    }} else {{
                      if (!fs.existsSync(dstPath)) {{
                        fs.copyFileSync(srcPath, dstPath);
                      }}
                    }}
                  }}
                }};
                restoreDir(uploadsBackup, uploadsDir);
                console.log('[webhook] public/uploads/ restored from backup');
              }} catch (e) {{
                console.error('[webhook] Failed to restore uploads:', e.message);
              }}
            }}

            // 4. Восстанавливаем .env если git его перезаписал
            restoreEnv(envBackup);

            if (pullErr) {{
              console.error('[webhook] Aborting: git fetch/reset failed');
              return;
            }}

            // 4a. Читаем DATABASE_URL из .env для передачи в prisma и build
            // Не используем source .env — он ломается на спецсимволах в паролях
            const dbUrlLine = fs.existsSync(ENV_FILE)
              ? fs.readFileSync(ENV_FILE, 'utf8').split('\\n').find(l => l.startsWith('DATABASE_URL='))
              : null;
            const dbUrlValue = dbUrlLine ? dbUrlLine.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '') : '';
            const dbEnvPrefix = dbUrlValue ? `DATABASE_URL='${{dbUrlValue}}' ` : '';

            // 5. npm install
            runCmd(`cd ${{SITE_PATH}} && npm install --legacy-peer-deps 2>&1`, 'npm install', (npmErr) => {{
              if (npmErr) {{
                console.error('[webhook] Aborting: npm install failed');
                return;
              }}

              // 6. prisma generate + migrate deploy (если есть prisma схема)
              // ВАЖНО: используем migrate deploy, а НЕ migrate dev!
              // migrate dev может пересоздать БД и удалить данные!
              // migrate deploy только применяет новые миграции без потери данных
              const hasPrisma = fs.existsSync(path.join(SITE_PATH, 'prisma', 'schema.prisma'));
              if (hasPrisma) {{
                runCmd(`cd ${{SITE_PATH}} && ${{dbEnvPrefix}}npx prisma generate 2>&1`, 'prisma generate', (prismaErr) => {{
                  if (prismaErr) {{
                    console.warn('[webhook] prisma generate failed (non-fatal):', prismaErr.message);
                  }}
                  // prisma migrate deploy — безопасно, только применяет ожидающие миграции
                  const hasMigrations = fs.existsSync(path.join(SITE_PATH, 'prisma', 'migrations'));
                  if (hasMigrations) {{
                    runCmd(`cd ${{SITE_PATH}} && ${{dbEnvPrefix}}npx prisma migrate deploy 2>&1`, 'prisma migrate deploy', (migrateErr) => {{
                      if (migrateErr) {{
                        console.warn('[webhook] prisma migrate deploy failed:', migrateErr.message);
                      }}
                      doBuild(prevCommit, dbEnvPrefix);
                    }});
                  }} else {{
                    doBuild(prevCommit, dbEnvPrefix);
                  }}
                }});
              }} else {{
                doBuild(prevCommit, dbEnvPrefix);
              }}
            }});
          }});
        }});

        function doBuild(prevCommit, dbEnvPrefix) {{
          // 7. next build
          // DATABASE_URL уже прочитан выше (dbEnvPrefix), используем его
          runCmd(`cd ${{SITE_PATH}} && ${{dbEnvPrefix}}NODE_OPTIONS='--max-old-space-size=4096' npx next build 2>&1`, 'next build', (buildErr) => {{
            if (buildErr) {{
              console.error('[webhook] Build failed!');
              // Откатываемся на предыдущий коммит при ошибке сборки
              if (prevCommit) {{
                console.log('[webhook] Rolling back to previous commit:', prevCommit);
                runCmd(`chattr -R -i ${{SITE_PATH}}/ 2>/dev/null; cd ${{SITE_PATH}} && git reset --hard ${{prevCommit}} 2>&1`, 'rollback', () => {{
                  restoreEnv(envBackup);
                  console.log('[webhook] Rolled back to:', prevCommit);
                }});
              }}
              return;
            }}

            // 8. Копируем .env, static, db, prisma в standalone
            copyToStandalone();

            // 9. pm2 delete + start (вместо restart — надёжнее, обновляет env)
            runCmd(`pm2 delete ${{PROJECT}} 2>/dev/null; cd ${{SITE_PATH}} && HOSTNAME=0.0.0.0 HOST=0.0.0.0 pm2 start ecosystem.config.js --update-env 2>&1`, 'pm2 delete + start', (pm2Err) => {{
              if (pm2Err) {{
                console.error('[webhook] pm2 start failed');
              }} else {{
                runCmd('pm2 save', 'pm2 save', () => {{}});
                // Восстанавливаем immutable-защиту на .env и ecosystem.config.js
                // (как autodeploy.py делает в блоке 11 после деплоя)
                const envPath = path.join(SITE_PATH, '.env');
                const ecoPath = path.join(SITE_PATH, 'ecosystem.config.js');
                for (const f of [envPath, ecoPath]) {{
                  if (fs.existsSync(f)) {{
                    runCmd(`chattr +i ${{f}} 2>/dev/null`, 'chattr +i (restore protection)', () => {{}});
                  }}
                }}
                console.log('[webhook] Deploy completed successfully');
              }}
            }});
          }});
        }}

        res.writeHead(200, {{'Content-Type': 'application/json'}});
        res.end(JSON.stringify({{ status: 'ok', message: 'Deploy started' }}));
      }} catch(e) {{
        console.error('[webhook] Error processing request:', e.message);
        res.writeHead(400, {{'Content-Type': 'application/json'}});
        res.end(JSON.stringify({{ status: 'error', message: 'Bad request' }}));
      }}
    }});
  }} else if (req.method === 'GET' && req.url === '/health') {{
    res.writeHead(200, {{'Content-Type': 'application/json'}});
    res.end(JSON.stringify({{ status: 'ok', project: PROJECT, port: PORT, branch: GIT_BRANCH }}));
  }} else {{
    res.writeHead(404);
    res.end('Not found');
  }}
}});

server.listen(PORT, '0.0.0.0', () => {{
  console.log(`Webhook server for ${{PROJECT}} listening on port ${{PORT}} (branch: ${{GIT_BRANCH}}, mode: ${{RUN_MODE}})`);
}});

// Graceful shutdown
process.on('SIGINT', () => {{
  console.log('[webhook] Shutting down...');
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000);
}});
process.on('SIGTERM', () => {{
  console.log('[webhook] SIGTERM received, shutting down...');
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000);
}});

// Prevent crashes from uncaught errors
process.on('uncaughtException', (e) => {{
  console.error('[webhook] Uncaught exception:', e.message);
}});
process.on('unhandledRejection', (e) => {{
  console.error('[webhook] Unhandled rejection:', e);
}});
"""
        webhook_path = os.path.join(site_path, "webhook-server.js")
        try:
            with open(webhook_path, 'w') as f:
                f.write(webhook_script)

            # Удаляем старый webhook процесс если есть
            webhook_pm2_name = f"{pm2_name}-webhook"
            run_cmd(f"pm2 delete {webhook_pm2_name} 2>/dev/null")

            # Также проверяем и удаляем основной PM2-процесс если он
            # указывает на другую директорию (конфликт точка ↔ дефис)
            try:
                rc_chk, out_chk, _ = run_cmd(f"pm2 describe {pm2_name} 2>/dev/null")
                if rc_chk == 0 and out_chk:
                    import re as _re
                    _m = _re.search(r'exec cwd\s+│\s+(\S+)', out_chk)
                    if _m and _m.group(1).rstrip('/') != site_path.rstrip('/'):
                        console.print(
                            f"  [yellow]⚠ Webhook: PM2 '{pm2_name}' указывает на "
                            f"{_m.group(1)}, а нужно {site_path}. Удаляю...[/yellow]"
                        )
                        run_cmd(f"pm2 delete {pm2_name} 2>/dev/null")
                        run_cmd(f"pm2 delete {webhook_pm2_name} 2>/dev/null")
                        run_cmd("pm2 save 2>/dev/null")
            except Exception:
                pass

            rc, out, err = run_cmd(
                f"cd {site_path} && pm2 start webhook-server.js "
                f"--name {webhook_pm2_name}"
            )
            run_cmd("pm2 save")

            webhook_details = f"Файл: {webhook_path}\nWebhook порт: {webhook_port}"

            if repo_name and github_token:
                # Определяем внешний IP сервера
                rc_ip, server_ip, _ = run_cmd(
                    "curl -s --max-time 5 https://api.ipify.org 2>/dev/null || "
                    "hostname -I | awk '{print $1}'"
                )
                server_ip = server_ip.strip()
                if not server_ip:
                    server_ip = "REPLACE_WITH_YOUR_IP"

                webhook_url = f"http://{server_ip}:{webhook_port}/webhook"

                # Сначала проверяем — нет ли уже вебхука с таким URL?
                rc_list, out_list, _ = run_cmd(
                    f"curl -s -H 'Authorization: token {github_token}' "
                    f"-H 'Accept: application/vnd.github.v3+json' "
                    f"https://api.github.com/repos/{repo_name}/hooks",
                    timeout=10
                )
                hook_exists = False
                duplicate_ids = []
                if rc_list == 0 and out_list:
                    try:
                        existing_hooks = json.loads(out_list)
                        for h in existing_hooks:
                            h_url = h.get("config", {}).get("url", "")
                            h_id = h.get("id")
                            if h_url == webhook_url:
                                hook_exists = True
                            elif "/webhook" in h_url and h_url.startswith(f"http://{server_ip}:"):
                                # Вебхук на этот сервер но с другим портом —
                                # вероятно от предыдущего деплоя, удаляем
                                duplicate_ids.append(h_id)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Удаляем устаревшие вебхуки от предыдущих деплоев
                for dup_id in duplicate_ids:
                    run_cmd(
                        f"curl -s -X DELETE "
                        f"-H 'Authorization: token {github_token}' "
                        f"https://api.github.com/repos/{repo_name}/hooks/{dup_id}",
                        timeout=5
                    )
                    console.print(f"  [dim]Удалён устаревший webhook #{dup_id}[/dim]")

                if hook_exists:
                    webhook_details += f"\nWebhook URL: {webhook_url}"
                    webhook_details += "\nWebhook уже зарегистрирован в GitHub"
                else:
                    # Формируем JSON для GitHub API через json.dumps (безопасно)
                    # Добавляем secret чтобы GitHub отправлял X-Hub-Signature-256
                    webhook_payload = json.dumps({
                        "name": "web",
                        "active": True,
                        "events": ["push"],
                        "config": {
                            "url": webhook_url,
                            "content_type": "json",
                            "secret": secret
                        }
                    })

                    # Записываем payload во временный файл чтобы избежать
                    # проблем с экранированием в shell
                    tmp_dir = os.path.join(site_path, "tmp")
                    os.makedirs(tmp_dir, exist_ok=True)
                    payload_file = os.path.join(tmp_dir, "_webhook_payload.json")
                    with open(payload_file, 'w') as f:
                        f.write(webhook_payload)

                    rc_hook, out_hook, err_hook = run_cmd(
                        f"curl -s -w '\\n%{{http_code}}' -X POST "
                        f"-H 'Authorization: token {github_token}' "
                        f"-H 'Accept: application/vnd.github.v3+json' "
                        f"-H 'Content-Type: application/json' "
                        f"https://api.github.com/repos/{repo_name}/hooks "
                        f"-d @{payload_file}",
                        timeout=15
                    )

                    # Удаляем временный файл
                    try:
                        os.remove(payload_file)
                    except OSError:
                        pass

                    # Разбираем ответ: последняя строка — HTTP status code
                    hook_lines = out_hook.strip().split('\n') if out_hook else []
                    hook_status = hook_lines[-1].strip() if hook_lines else "000"

                    if hook_status == "201":
                        webhook_details += f"\nWebhook URL: {webhook_url}"
                        webhook_details += "\nWebhook зарегистрирован в GitHub"
                    else:
                        # Попытка парсинга ошибки из JSON-ответа
                        hook_body = '\n'.join(hook_lines[:-1]) if len(hook_lines) > 1 else out_hook or ""
                        error_msg = ""
                        try:
                            error_data = json.loads(hook_body)
                            error_msg = error_data.get("message", "")
                            if error_data.get("errors"):
                                for e in error_data["errors"]:
                                    error_msg += f" | {e.get('message', str(e))}"
                        except (json.JSONDecodeError, TypeError):
                            error_msg = hook_body[:200]

                        webhook_details += f"\nWebhook URL: {webhook_url}"
                        webhook_details += f"\nWebhook НЕ зарегистрирован (HTTP {hook_status})"
                        if error_msg:
                            webhook_details += f"\nПричина: {error_msg[:200]}"
                        # Не считаем ошибкой всего блока — webhook можно добавить вручную
                        console.print(
                            f"  [yellow]Внимание: webhook не удалось зарегистрировать в GitHub "
                            f"(HTTP {hook_status})[/yellow]"
                        )
                        console.print(
                            f"  [yellow]Добавьте вручную: {webhook_url}[/yellow]"
                        )

            self.ctx["webhook_port"] = webhook_port
            return self._add_result(15, "autodeploy", "Настройка автодеплоя", True,
                                    f"Webhook порт: {webhook_port}",
                                    webhook_details)
        except Exception as e:
            return self._add_result(15, "autodeploy", "Настройка автодеплоя", False,
                                    f"Ошибка: {e}")

    # ─── Маршрутизатор ──────────────────────────────────────────────────

    def execute_block(self, num: int) -> BlockResult:
        dispatch = {
            1: self.execute_block1,
            2: self.execute_block2,
            3: self.execute_block3,
            4: self.execute_block4,
            5: self.execute_block5,
            6: self.execute_block6,
            7: self.execute_block7,
            8: self.execute_block8,
            9: self.execute_block9,
            10: self.execute_block10,
            11: self.execute_block11,
            12: self.execute_block12,
            13: self.execute_block13,
            14: self.execute_block14,
            15: self.execute_block15,
        }
        handler = dispatch.get(num)
        if handler:
            return handler()
        return self._add_result(num, f"block{num}", f"Блок {num}", False,
                                "Неизвестный блок")

    def get_summary_text(self) -> str:
        """Текстовая сводка."""
        lines = []
        lines.append("=" * 60)
        lines.append("  СВОДКА РЕЗУЛЬТАТОВ АВТОДЕПЛОЯ")
        lines.append("=" * 60)
        lines.append("")

        ok = 0
        err = 0
        err_block = None

        for r in self.results:
            marker = "OK" if r.success else "ОШИБКА"
            lines.append(f"  {'✔' if r.success else '✘'} Блок {r.block_num:2d} | {r.title:35s} | {marker}")
            lines.append(f"         {r.message}")
            if r.details:
                for dl in r.details.split('\n'):
                    lines.append(f"           {dl}")
            lines.append("")
            if r.success:
                ok += 1
            else:
                err += 1
                if err_block is None:
                    err_block = r.block_num

        lines.append("-" * 60)
        lines.append(f"  Успешно: {ok}  |  Ошибки: {err}")
        if err_block:
            lines.append(f"  Первая ошибка в блоке: {err_block}")
        else:
            lines.append("  Все блоки выполнены успешно!")
        lines.append("=" * 60)
        return '\n'.join(lines)

    def save_log(self, base_dir: str = None):
        """Сохраняет лог в deploy-logs/."""
        if base_dir is None:
            base_dir = os.getcwd()
        log_dir = os.path.join(base_dir, "deploy-logs")
        os.makedirs(log_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project = self.ctx.get("project_name", "unknown")

        # .log
        log_file = os.path.join(log_dir, f"deploy_{project}_{timestamp}.log")
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(self.get_summary_text())
            f.write("\n\n--- КОНТЕКСТ ---\n")
            safe = {k: ("***" if ('password' in k.lower() or 'token' in k.lower()) and v else v)
                    for k, v in self.ctx.items() if not k.startswith('_')}
            f.write(json.dumps(safe, ensure_ascii=False, indent=2))

        # .json
        json_file = os.path.join(log_dir, f"deploy_{project}_{timestamp}.json")
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump({
                "timestamp": timestamp,
                "project": project,
                "results": [{"block": r.block_num, "title": r.title,
                             "success": r.success, "message": r.message}
                            for r in self.results],
                "context": safe,
            }, f, ensure_ascii=False, indent=2)

        return log_file


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ОПИСАНИЯ БЛОКОВ                                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

BLOCK_INFO = [
    (1,  "Название проекта и путь",       "MANUAL",  "Ввод данных"),
    (2,  "Выбор свободного порта",         "AUTO",    "Автоматически"),
    (3,  "Режим запуска (Standalone/Std)", "MANUAL",  "Выбор режима"),
    (4,  "Домен и SSL",                    "MANUAL",  "Ввод данных"),
    (5,  "Подготовка PM2 конфигурации",    "AUTO",    "Автоматически"),
    (6,  "Конфигурация веб-сервера",       "AUTO",    "Автоматически"),
    (7,  "Git-репозиторий",               "MANUAL",  "Ввод данных"),
    (8,  "Поиск файла базы данных",        "AUTO",    "Автоматически"),
    (9,  "Создание .gitignore",            "AUTO",    "Автоматически"),
    (10, "Создание .env",                  "AUTO",    "Автоматически"),
    (11, "Сборка и запуск",               "AUTO",    "npm install + next build + pm2 start"),
    (12, "Проверка репозитория и токена",  "CHECK",   "Авто + запрос при ошибке"),
    (13, "Первый коммит и пуш",            "AUTO",    "Автоматически"),
    (14, "Финальные проверки",             "AUTO",    "Автоматически"),
    (15, "Настройка автодеплоя",           "AUTO",    "Автоматически"),
]


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ОСНОВНОЙ ПОТОК                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def print_banner():
    console.print()
    console.print(Panel.fit(
        "[bold green]AutoDeploy[/bold green] — Автоматический деплой сайтов\n"
        "[dim]15 блоков. Большинство выполняются автоматически.[/dim]",
        border_style="green"
    ))
    console.print()


def print_block_header(num: int, title: str, block_type: str):
    """Красивый заголовок блока."""
    type_colors = {"AUTO": "cyan", "MANUAL": "yellow", "CHECK": "magenta"}
    color = type_colors.get(block_type, "white")
    console.print()
    console.print(Rule(
        f"[bold {color}]Блок {num}/15: {title}  [{block_type}][/bold {color}]",
        style=color
    ))


def print_result(result: BlockResult):
    """Вывод результата блока."""
    if result.success:
        console.print(f"  [bold green]  OK[/bold green]  {result.message}")
    else:
        console.print(f"  [bold red]ОШИБКА[/bold red]  {result.message}")
    if result.details:
        for line in result.details.split('\n'):
            console.print(f"         [dim]{line}[/dim]")


def print_final_summary(executor: DeployExecutor, ctx: Dict[str, Any]):
    """Финальная сводка."""
    console.print()
    console.print(Rule("[bold]СВОДКА РЕЗУЛЬТАТОВ[/bold]"))

    has_errors = any(not r.success for r in executor.results)
    if has_errors:
        console.print("[bold red]  Есть ошибки![/bold red]")
    else:
        console.print("[bold green]  Все блоки выполнены успешно![/bold green]")
    console.print()

    table = Table(box=box.ROUNDED, show_lines=False)
    table.add_column("#", style="bold", width=3)
    table.add_column("Блок", width=35)
    table.add_column("Статус", width=8)
    table.add_column("Результат", width=50)

    for r in executor.results:
        status = "[green]OK[/green]" if r.success else "[red]ОШИБКА[/red]"
        table.add_row(str(r.block_num), r.title, status, r.message)

    console.print(table)

    # Ключевые данные
    console.print()
    console.print("[bold]Ключевые параметры:[/bold]")
    key_data = [
        ("Проект", ctx.get("project_name", "")),
        ("Домен", ctx.get("domain", "")),
        ("Путь", ctx.get("site_path", "")),
        ("Порт приложения", str(ctx.get("app_port", ""))),
        ("Git ветка", ctx.get("git_branch", "")),
        ("Файл БД", ctx.get("db_file", "не найден")),
        ("Репозиторий", ctx.get("repo_name", "")),
        ("Webhook порт", str(ctx.get("webhook_port", ""))),
    ]
    for k, v in key_data:
        if v:
            console.print(f"  [cyan]{k}:[/cyan] {v}")

    # Лог
    log_file = executor.save_log()
    console.print(f"\n  [dim]Лог: {log_file}[/dim]")


def main():
    ctx: Dict[str, Any] = {}
    executor = DeployExecutor(ctx)

    print_banner()

    # ── Список блоков ────────────────────────────────────────────────────
    console.print("[bold]План выполнения:[/bold]")
    console.print()
    for num, title, btype, desc in BLOCK_INFO:
        type_colors = {"AUTO": "cyan", "MANUAL": "yellow", "CHECK": "magenta"}
        color = type_colors.get(btype, "white")
        console.print(f"  {num:2d}. [{color}]{btype:6s}[/{color}]  {title:35s}  [dim]{desc}[/dim]")
    console.print()
    console.print("[dim]Нажмите Enter для начала...[/dim]")
    input()

    # ── Блок 1: Проект и путь ────────────────────────────────────────────
    print_block_header(1, "Название проекта и путь", "MANUAL")
    console.print("Укажите название проекта и путь к директории сайта.")
    console.print("Путь — корневая директория сайта (например: /var/www/site.ru).")
    console.print()

    ctx["project_name"] = Prompt.ask("  [bold yellow]Название проекта[/bold yellow]")

    # Подсказка для пути: для кириллических доменов показываем Punycode,
    # т.к. именно он будет реально использоваться в файловой системе
    _path_hint = ctx.get('project_name', 'mysite')
    _punycode_hint = domain_to_punycode(_path_hint)
    if _punycode_hint != _path_hint:
        _path_display = f"/var/www/{_punycode_hint} (Punycode для {_path_hint})"
    else:
        _path_display = f"/var/www/{_path_hint}"

    ctx["site_path"] = Prompt.ask("  [bold yellow]Путь к сайту[/bold yellow]",
                                  default=f"/var/www/{_punycode_hint}")

    result = executor.execute_block(1)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 2: Свободный порт (авто) ────────────────────────────────────
    print_block_header(2, "Выбор свободного порта", "AUTO")
    console.print("Ищу свободный порт...")
    result = executor.execute_block(2)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 3: Режим запуска ─────────────────────────────────────────────
    print_block_header(3, "Режим запуска (Standalone / Standard)", "MANUAL")
    console.print("  [bold]Выберите режим запуска:[/bold]")
    console.print("  [green][1][/green] Standalone — запуск через [cyan]node .next/standalone/server.js[/cyan]")
    console.print("      (быстрее, меньше памяти, не требует node_modules для запуска)")
    console.print("      Требуется output: \"standalone\" в next.config.ts/js")
    console.print("  [green][2][/green] Standard — запуск через [cyan]npx next start -p PORT[/cyan]")
    console.print("      (требует полный node_modules, проще для отладки)")
    console.print("  [green][3][/green] Автоопределение после сборки")
    console.print()

    ctx["use_standalone"] = Prompt.ask("  [bold yellow]Ваш выбор[/bold yellow]",
                                       choices=["1", "2", "3"], default="3")

    result = executor.execute_block(3)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 4: Домен и SSL ─────────────────────────────────────────────
    print_block_header(4, "Домен и SSL", "MANUAL")
    console.print("  [dim]Можно вводить кириллицей (пункт.рф) — скрипт автоматически[/dim]")
    console.print("  [dim]конвертирует в Punycode для Nginx и DNS.[/dim]")
    ctx["site_domain"] = Prompt.ask("  [bold yellow]Домен[/bold yellow] (Enter чтобы пропустить)",
                                    default="")
    if ctx["site_domain"]:
        ctx["use_ssl"] = "yes" if Confirm.ask(
            "  [bold yellow]Настроить SSL (Let's Encrypt)?[/bold yellow]",
            default=False) else "no"
    else:
        ctx["use_ssl"] = "no"
        console.print("  [dim]Домен не указан — Apache не будет настроен[/dim]")

    result = executor.execute_block(4)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блоки 5-6: PM2 конфиг + Веб-сервер (авто) ────────────────────────
    for block_num in [5, 6]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        if not result.success:
            print_final_summary(executor, ctx)
            return

    # ── Блок 7: Репозиторий ──────────────────────────────────────────────
    print_block_header(7, "Git-репозиторий", "MANUAL")
    console.print("Укажите данные GitHub-репозитория.")
    console.print("Формат: владелец/имя (например: [cyan]MyUser/be2st.ru[/cyan])")
    console.print()

    ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow] (логин GitHub)")
    ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]")
    ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
    ctx["repo_private"] = Confirm.ask("  [bold yellow]Репозиторий приватный?[/bold yellow]",
                                      default=True)
    ctx["github_token"] = Prompt.ask("  [bold yellow]GitHub Token[/bold yellow]",
                                     password=True)

    result = executor.execute_block(7)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блоки 8-10: Авто ──────────────────────────────────────────────────
    for block_num in [8, 9, 10]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        if not result.success:
            print_final_summary(executor, ctx)
            return

    # ── Блок 11: Сборка и запуск (отдельно — долгий процесс) ─────────────
    _, title, btype, _ = BLOCK_INFO[10]
    print_block_header(11, title, btype)
    console.print("[bold]Это может занять несколько минут...[/bold]")
    console.print()
    result = executor.execute_block(11)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 12: Проверка репо и токена (check) ──────────────────────────
    while True:
        print_block_header(12, "Проверка репозитория и токена", "CHECK")
        console.print("Проверяю доступ к репозиторию...")
        result = executor.execute_block(12)
        print_result(result)
        if result.success:
            break
        # Ошибка — запрашиваем исправления
        console.print()
        console.print("[bold red]Не удалось получить доступ к репозиторию.[/bold red]")
        console.print("Возможные причины: неверный токен, нет прав, репозиторий не найден.")
        console.print("[yellow]Если репозиторий ещё не создан — создайте его вручную на github.com[/yellow]")
        console.print()
        console.print("  [green][1][/green] — Только токен")
        console.print("  [green][2][/green] — Владельца и имя репозитория")
        console.print("  [green][3][/green] — Всё (владелец, имя, токен)")
        retry_what = Prompt.ask(
            "  [bold yellow]Что исправить?[/bold yellow]",
            choices=["1", "2", "3"],
            default="1"
        )
        if retry_what == "1":
            ctx["github_token_retry"] = Prompt.ask("  [bold yellow]Новый GitHub Token[/bold yellow]",
                                                    password=True)
        elif retry_what == "2":
            ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow]",
                                           default=ctx.get("repo_owner", ""))
            ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]",
                                                default=ctx.get("repo_short_name", ""))
            ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
        elif retry_what == "3":
            ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow]",
                                           default=ctx.get("repo_owner", ""))
            ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]",
                                                default=ctx.get("repo_short_name", ""))
            ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
            ctx["github_token_retry"] = Prompt.ask("  [bold yellow]Новый GitHub Token[/bold yellow]",
                                                    password=True)
        # Удаляем старый неудачный результат чтобы не дублировать в сводке
        if executor.results and executor.results[-1].block_num == 12:
            executor.results.pop()

    # ── Блоки 13-15: Авто ────────────────────────────────────────────────
    for block_num in [13, 14, 15]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        # Блок 14 (финальные проверки) — информационный, не блокирует
        # последующие блоки. Прерываем только при ошибке в критичных блоках.
        if not result.success and block_num != 14:
            break

    # ── Финальная сводка ─────────────────────────────────────────────────
    print_final_summary(executor, ctx)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[bold red]Прервано пользователем.[/bold red]")
        sys.exit(1)

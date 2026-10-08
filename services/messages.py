"""Friendly presentation only. Stored event codes and security decisions stay unchanged."""
import re

BRAND = "КПП ТОРТИК • дежурный на связи"
FOOTER = "ТОРТИК PROJECT • бережём чат, выдаём пропуска"


def captcha_intro(question, ttl):
    return ("О, я до тебя достучался! Ты на КПП уже будто 3 часа стоишь 😄\n\n"
            "**Пройди биометрию:**\n"
            f"{question}\n\n"
            "Шучу, никакой настоящей биометрии — только простой пример. "
            "Это наша защита от ботов и спам-налётов, чтобы в ТОРТИК было уютно.\n\n"
            "Жми кнопку ниже и вводи ответ. Как решишь — сниму ограничение проверки и выдам роли. "
            "Семь дней ждать не надо!\n"
            f"На этот пример есть {ttl} сек. Если истечёт — кнопка выдаст новый.")


EVENT_TITLES = {
    "captcha_passed": "Биометрия сошлась — пропуск готов!",
    "captcha_reset": "Пропуск сбросили, ждём новую проверку",
    "verification_timeout": "Новичок на КПП — ждём ответ на пример",
    "verification_dm_closed": "В личку не достучался",
    "verification_delivery_error": "Сообщение застряло по дороге",
    "captcha_error": "Сканер примеров споткнулся",
    "verification_view_error": "Кнопке проверки нужна помощь",
    "manual_verified": "Сотрудник выдал пропуск вручную",
    "manual_verify_role_repair_required": "Пропуск есть, роли ещё поправим",
    "manual_verify_skipped": "Этот пропуск пока пропустили",
    "manual_verify_error": "Ручная проверка споткнулась",
    "verify_all_started": "Начали раздачу пропусков",
    "verify_all_finished": "Раздача пропусков: итоги",
    "verify_all_stopped": "Раздача пропусков остановлена",
    "verify_all_timeout": "Discord задумался — раздачу остановили",
    "verify_all_error": "Раздаче пропусков нужна помощь",
    "role_repair_completed": "Полочка с ролями проверена",
    "role_repair_paused": "Сверка ролей уступила очередь",
    "roles_sync": "Поправили набор ролей",
    "syncall_requested": "Сотрудник заказал сверку ролей",
    "level_role_error": "Роль не выдалась — нужна помощь",
    "repair_error": "Сверка ролей споткнулась",
    "bot_permission_error": "У дежурного не хватает прав",
    "command_error": "Команда споткнулась",
    "warn": "Записали предупреждение",
    "warning_removed": "Сняли предупреждение",
    "warnings_cleared": "Почистили список предупреждений",
    "profanity_deleted": "Убрали сообщение с запрещёнными словами",
    "timeout": "Пауза в чате: выдан timeout",
    "untimeout": "Сняли timeout модерации",
    "kick": "Участника исключили с сервера",
    "ban": "Участника заблокировали",
    "unban": "Блокировку сняли",
    "purge": "Навели порядок в сообщениях",
    "userinfo": "Карточка участника",
    "xp_changed": "Обновили запас опыта",
    "staff_attention": "Дежурные, тут нужна ваша помощь",
    "escalation_blocked": "Автонаказание не применено: проверьте права",
    "escalation_error": "Автонаказание не удалось применить",
}


def event_title(action):
    return EVENT_TITLES.get(action, action)


def event_detail(action, detail):
    if action == "verification_timeout":
        return "Ограничение проверки — до 7 дней. После правильного ответа снимается сразу."
    if action == "verification_dm_closed":
        return "ЛС закрыты. Нужны открытые ЛС и повторная отправка через /verification resend."
    if action == "manual_verified":
        return "Сотрудник подтвердил участника без captcha."
    if action == "captcha_passed":
        return "Роли на месте, добро пожаловать!" if detail == "Roles synchronized" else "Проверка пройдена, но часть ролей требует помощи."
    if action == "role_repair_completed":
        match = re.fullmatch(r"processed=(\d+); errors=(\d+)", detail)
        if match:
            return f"Проверено участников: {match[1]}. Ошибок: {match[2]}."
    if action == "verify_all_finished":
        for key, label in (("roles_ok=", "Готово: "), ("role_repair=", "Поправить роли: "),
                           ("skipped=", "Пропущено: "), ("errors=", "Ошибок: ")):
            detail = detail.replace(key, label)
    return detail


def participant_label(uid):
    return str(uid) if uid else "весь сервер"


def actor_label(uid):
    return str(uid) if uid else "дежурный-бот"


def verification_label(state):
    return {"pending": "ждём на КПП", "passed": "пример решён, оформляем пропуск",
            "verified": "пропуск на руках"}.get(state, "ещё не проходил КПП")

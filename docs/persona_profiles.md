# Формат persona‑профиля

Формат профиля — JSON‑объект. Минимальный набор полей предназначен для явной изоляции профилей и предсказуемого поведения валидатора.

## Поля
- `id` (string, ≤ 64): уникальный идентификатор профиля.
- `name` (string, ≤ 64): имя персоны.
- `age` (int, 1..120)
- `gender` ("male" | "female")
- `zodiac` (Aries, Taurus, …)
- `temperament` (object): `{sanguine, choleric, phlegmatic, melancholic}`; значения 0..1, сумма ≈ 1.0.
- `sociality` ("introvert" | "ambivert" | "extrovert")
- `archetypes` (array<string>): список архетипов.
- `role` (string, ≤ 1000): ролевая установка/описание.

## Пример
```json
{
  "id": "persona_main",
  "name": "Bonnie",
  "age": 24,
  "gender": "female",
  "zodiac": "Libra",
  "temperament": {
    "sanguine": 0.4,
    "choleric": 0.25,
    "phlegmatic": 0.2,
    "melancholic": 0.15
  },
  "sociality": "extrovert",
  "archetypes": ["Rebel", "Jester", "Sage"],
  "role": "Playful, confident companion persona."
}
```

## Проверка
```
python scripts/validate_persona_profile.py <profile.json>
```

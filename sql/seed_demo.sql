-- seed_demo.sql — СИНТЕТИЧЕСКИЕ демонстрационные данные.
--
-- Реальные журналы ПСС содержат адреса и обстоятельства происшествий, поэтому
-- в репозиторий они не попадают. Этот скрипт генерирует правдоподобный набор,
-- чтобы `docker compose up` давал сразу работающее приложение, а eval-прогон
-- можно было выполнить, не имея доступа к продуктивным данным.
--
-- setseed фиксирует генератор: набор воспроизводим, а значит воспроизводимы
-- и цифры в отчёте eval/report.json.

SELECT setseed(0.42);

INSERT INTO pss_departures (
    record_id, journal_type, source_file, date,
    time_notify, time_depart, time_arrive, time_return,
    duration_travel_min, duration_total_min,
    pss_unit, incident_type, address, district, object_type, result,
    victims, evacuated, personnel_pss, vehicles_pss,
    fire_vehicles, incident_vehicles, other_services,
    special_notes, description_raw
)
SELECT
    'D-' || lpad(i::text, 5, '0'),
    'Журнал выездов',
    'demo_seed.sql',
    DATE '2025-01-01' + (floor(random() * 365))::int,
    notify,
    (notify + make_interval(mins => 1 + floor(random() * 5)::int))::time,
    (notify + make_interval(mins => travel))::time,
    (notify + make_interval(mins => travel + 20 + floor(random() * 90)::int))::time,
    travel,
    travel + 20 + floor(random() * 90)::int,
    (ARRAY['ПСО-1', 'ПСО-2', 'ПСО-3', 'ПСО-4', 'Аварийно-спасательный отряд'])
        [1 + floor(random() * 5)::int],
    incident,
    'ул. Демонстрационная, д. ' || (1 + floor(random() * 120))::int,
    (ARRAY['Центральный', 'Советский', 'Кировский', 'Ленинский', 'Октябрьский'])
        [1 + floor(random() * 5)::int],
    (ARRAY['жилой дом', 'школа', 'автодорога', 'производственный объект', 'открытая местность'])
        [1 + floor(random() * 5)::int],
    (ARRAY['Помощь оказана', 'Ложный вызов', 'Ликвидировано', 'Передано другой службе'])
        [1 + floor(random() * 4)::int],
    CASE WHEN incident LIKE 'ДТП%' THEN floor(random() * 4)::int
         WHEN incident LIKE 'Пожар%' THEN floor(random() * 3)::int
         ELSE 0 END,
    CASE WHEN random() < 0.25 THEN floor(random() * 15)::int ELSE 0 END,
    2 + floor(random() * 6)::int,
    1 + floor(random() * 3)::int,
    '[]'::jsonb,
    '[]'::jsonb,
    CASE WHEN random() < 0.3
         THEN '[{"service": "Скорая помощь"}]'::jsonb
         ELSE '[]'::jsonb END,
    CASE WHEN random() < 0.1 THEN 'Требуется дополнительная проверка' ELSE NULL END,
    'Выезд по сообщению. ' || incident || '. Проведены аварийно-спасательные работы.'
FROM (
    SELECT
        i,
        (TIME '00:00' + make_interval(secs => floor(random() * 86400)::int)) AS notify,
        5 + floor(random() * 25)::int AS travel,
        (ARRAY[
            'ДТП (столкновение)',
            'ДТП (наезд)',
            'Пожар в жилом секторе',
            'Сигнализация ложная',
            'Поиск человека',
            'Деблокирование двери',
            'Происшествие на воде'
        ])[1 + floor(random() * 7)::int] AS incident
    FROM generate_series(1, 600) AS i
) src
ON CONFLICT (record_id) DO NOTHING;


INSERT INTO pss_lessons (
    record_id, journal_type, source_file, date,
    time_start, time_end, duration_min, duration_planned_min,
    pss_unit, lesson_type, normative, location, district,
    participants_count, equipment_used, result, lesson_desc_raw
)
SELECT
    'L-' || lpad(i::text, 5, '0'),
    'Журнал занятий',
    'demo_seed.sql',
    DATE '2025-01-01' + (floor(random() * 365))::int,
    start_time,
    (start_time + make_interval(mins => duration))::time,
    duration,
    planned,
    (ARRAY['ПСО-1', 'ПСО-2', 'ПСО-3', 'ПСО-4', 'Аварийно-спасательный отряд'])
        [1 + floor(random() * 5)::int],
    (ARRAY['Норматив', 'Теоретическое занятие', 'Практическая тренировка', 'Учения'])
        [1 + floor(random() * 4)::int],
    (ARRAY['Норматив №1 (боевое развёртывание)',
           'Норматив №4 (работа с ГАСИ)',
           'Норматив №7 (спасение с высоты)',
           'Норматив №12 (работа на воде)'])[1 + floor(random() * 4)::int],
    (ARRAY['Учебная башня', 'Спортзал', 'Полигон', 'Территория части'])
        [1 + floor(random() * 4)::int],
    (ARRAY['Центральный', 'Советский', 'Кировский', 'Ленинский', 'Октябрьский'])
        [1 + floor(random() * 5)::int],
    4 + floor(random() * 16)::int,
    '[]'::jsonb,
    (ARRAY['Норматив выполнен', 'Норматив выполнен с замечаниями', 'Занятие проведено'])
        [1 + floor(random() * 3)::int],
    'Проведено занятие согласно плану подготовки.'
FROM (
    SELECT
        i,
        (TIME '08:00' + make_interval(mins => floor(random() * 480)::int)) AS start_time,
        45 + floor(random() * 60)::int AS duration,
        60 AS planned
    FROM generate_series(1, 300) AS i
) src
ON CONFLICT (record_id) DO NOTHING;

ANALYZE pss_departures;
ANALYZE pss_lessons;

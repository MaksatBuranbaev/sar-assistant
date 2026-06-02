-- schema.sql — структура БД аналитического ассистента ПСС.
--
-- Описания колонок хранятся в COMMENT ON COLUMN, а не в Python: db.get_schema()
-- читает их из каталога и собирает описание схемы для промпта. Добавили колонку
-- с комментарием — ассистент узнал о ней без правки кода и релиза.

CREATE TABLE IF NOT EXISTS pss_departures (
    record_id           text PRIMARY KEY,
    journal_type        text,
    source_file         text,
    date                date,
    time_notify         time,
    time_depart         time,
    time_arrive         time,
    time_return         time,
    duration_travel_min integer,
    duration_total_min  integer,
    pss_unit            text,
    incident_type       text,
    address             text,
    district            text,
    object_type         text,
    result              text,
    victims             integer,
    evacuated           integer,
    personnel_pss       integer,
    vehicles_pss        integer,
    fire_vehicles       jsonb,
    incident_vehicles   jsonb,
    other_services      jsonb,
    special_notes       text,
    description_raw     text,
    radio_log_raw       text,
    units_raw           text,
    circumstances_raw   text,
    loaded_at           timestamptz DEFAULT now()
);

COMMENT ON TABLE  pss_departures IS 'журнал выездов поисково-спасательной службы';
COMMENT ON COLUMN pss_departures.record_id           IS 'номер записи в АИС';
COMMENT ON COLUMN pss_departures.journal_type        IS 'тип журнала';
COMMENT ON COLUMN pss_departures.source_file         IS 'имя исходного файла';
COMMENT ON COLUMN pss_departures.date                IS 'дата выезда';
COMMENT ON COLUMN pss_departures.time_notify         IS 'время получения извещения';
COMMENT ON COLUMN pss_departures.time_depart         IS 'время выезда';
COMMENT ON COLUMN pss_departures.time_arrive         IS 'время прибытия на место';
COMMENT ON COLUMN pss_departures.time_return         IS 'время возвращения в подразделение';
COMMENT ON COLUMN pss_departures.duration_travel_min IS 'время в пути, минуты';
COMMENT ON COLUMN pss_departures.duration_total_min  IS 'общая продолжительность выезда, минуты';
COMMENT ON COLUMN pss_departures.pss_unit            IS 'подразделение ПСС';
COMMENT ON COLUMN pss_departures.incident_type       IS 'тип происшествия';
COMMENT ON COLUMN pss_departures.address             IS 'адрес происшествия';
COMMENT ON COLUMN pss_departures.district            IS 'район / административный округ';
COMMENT ON COLUMN pss_departures.object_type         IS 'тип объекта';
COMMENT ON COLUMN pss_departures.result              IS 'результат выезда';
COMMENT ON COLUMN pss_departures.victims             IS 'количество пострадавших';
COMMENT ON COLUMN pss_departures.evacuated           IS 'количество эвакуированных';
COMMENT ON COLUMN pss_departures.personnel_pss       IS 'личный состав ПСС, человек';
COMMENT ON COLUMN pss_departures.vehicles_pss        IS 'техника ПСС, единиц';
COMMENT ON COLUMN pss_departures.fire_vehicles       IS 'пожарная техника, массив объектов';
COMMENT ON COLUMN pss_departures.incident_vehicles   IS 'техника по происшествию, массив объектов';
COMMENT ON COLUMN pss_departures.other_services      IS 'иные привлечённые службы, массив объектов';
COMMENT ON COLUMN pss_departures.special_notes       IS 'особые отметки';
COMMENT ON COLUMN pss_departures.description_raw     IS 'исходный текст описания из журнала';
COMMENT ON COLUMN pss_departures.radio_log_raw       IS 'журнал радиообмена';
COMMENT ON COLUMN pss_departures.units_raw           IS 'список привлечённых сил, сырой текст';
COMMENT ON COLUMN pss_departures.circumstances_raw   IS 'обстоятельства выезда, сырой текст';
COMMENT ON COLUMN pss_departures.loaded_at           IS 'время загрузки записи в БД';


CREATE TABLE IF NOT EXISTS pss_lessons (
    record_id            text PRIMARY KEY,
    journal_type         text,
    source_file          text,
    date                 date,
    time_start           time,
    time_end             time,
    duration_min         integer,
    pss_unit             text,
    lesson_type          text,
    normative            text,
    location             text,
    district             text,
    participants_count   integer,
    duration_planned_min integer,
    equipment_used       jsonb,
    result               text,
    special_notes        text,
    lesson_desc_raw      text,
    normative_raw        text,
    location_raw         text,
    unit_raw             text,
    loaded_at            timestamptz DEFAULT now()
);

COMMENT ON TABLE  pss_lessons IS 'журнал занятий и тренировок ПСС';
COMMENT ON COLUMN pss_lessons.record_id            IS 'уникальный идентификатор занятия';
COMMENT ON COLUMN pss_lessons.journal_type         IS 'тип журнала';
COMMENT ON COLUMN pss_lessons.source_file          IS 'имя исходного файла';
COMMENT ON COLUMN pss_lessons.date                 IS 'дата занятия';
COMMENT ON COLUMN pss_lessons.time_start           IS 'время начала';
COMMENT ON COLUMN pss_lessons.time_end             IS 'время окончания';
COMMENT ON COLUMN pss_lessons.duration_min         IS 'фактическая длительность, минуты';
COMMENT ON COLUMN pss_lessons.pss_unit             IS 'подразделение ПСС';
COMMENT ON COLUMN pss_lessons.lesson_type          IS 'вид занятия';
COMMENT ON COLUMN pss_lessons.normative            IS 'отрабатываемый норматив';
COMMENT ON COLUMN pss_lessons.location             IS 'место проведения';
COMMENT ON COLUMN pss_lessons.district             IS 'район';
COMMENT ON COLUMN pss_lessons.participants_count   IS 'количество участников';
COMMENT ON COLUMN pss_lessons.duration_planned_min IS 'плановая длительность, минуты';
COMMENT ON COLUMN pss_lessons.equipment_used       IS 'использованное снаряжение, массив объектов';
COMMENT ON COLUMN pss_lessons.result               IS 'результат занятия';
COMMENT ON COLUMN pss_lessons.special_notes        IS 'особые отметки';
COMMENT ON COLUMN pss_lessons.lesson_desc_raw      IS 'описание занятия, сырой текст';
COMMENT ON COLUMN pss_lessons.normative_raw        IS 'норматив, сырой текст';
COMMENT ON COLUMN pss_lessons.location_raw         IS 'место, сырой текст';
COMMENT ON COLUMN pss_lessons.unit_raw             IS 'подразделение, сырой текст';
COMMENT ON COLUMN pss_lessons.loaded_at            IS 'время загрузки записи в БД';


-- Индексы под фактические паттерны запросов ассистента: фильтр по периоду,
-- группировка по району, подразделению и типу происшествия.
CREATE INDEX IF NOT EXISTS idx_departures_date          ON pss_departures (date);
CREATE INDEX IF NOT EXISTS idx_departures_district      ON pss_departures (district);
CREATE INDEX IF NOT EXISTS idx_departures_incident_type ON pss_departures (incident_type);
CREATE INDEX IF NOT EXISTS idx_departures_unit          ON pss_departures (pss_unit);
CREATE INDEX IF NOT EXISTS idx_lessons_date             ON pss_lessons (date);
CREATE INDEX IF NOT EXISTS idx_lessons_unit             ON pss_lessons (pss_unit);

-- Поиск по сырому описанию идёт через ILIKE '%...%', а обычный B-tree такой
-- шаблон не поддерживает — нужен триграммный индекс.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_departures_description_trgm
    ON pss_departures USING gin (description_raw gin_trgm_ops);

-- 005_add_partial_dates_to_speakers.sql
-- Add MusicBrainz-style partial birth and death date columns (month and day)
-- so year-only, year+month, month+day without year, and full dates are
-- represented cleanly without placeholder values.

ALTER TABLE public.speakers
    ADD COLUMN IF NOT EXISTS birth_month SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS birth_day SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS death_month SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS death_day SMALLINT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_birth_month_range'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_birth_month_range CHECK (
                birth_month IS NULL OR birth_month BETWEEN 1 AND 12
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_birth_day_range'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_birth_day_range CHECK (
                birth_day IS NULL OR birth_day BETWEEN 1 AND 31
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_birth_day_requires_month'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_birth_day_requires_month CHECK (
                birth_day IS NULL OR birth_month IS NOT NULL
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_birth_calendar_valid'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_birth_calendar_valid CHECK (
                birth_day IS NULL OR (
                    CASE
                        WHEN birth_month IN (1, 3, 5, 7, 8, 10, 12) THEN birth_day <= 31
                        WHEN birth_month IN (4, 6, 9, 11) THEN birth_day <= 30
                        WHEN birth_month = 2 THEN
                            birth_day <= CASE
                                WHEN birth_year IS NULL
                                     OR (birth_year % 4 = 0 AND birth_year % 100 <> 0)
                                     OR (birth_year % 400 = 0)
                                THEN 29
                                ELSE 28
                            END
                        ELSE FALSE
                    END
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_death_month_range'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_death_month_range CHECK (
                death_month IS NULL OR death_month BETWEEN 1 AND 12
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_death_day_range'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_death_day_range CHECK (
                death_day IS NULL OR death_day BETWEEN 1 AND 31
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_death_day_requires_month'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_death_day_requires_month CHECK (
                death_day IS NULL OR death_month IS NOT NULL
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_death_calendar_valid'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_death_calendar_valid CHECK (
                death_day IS NULL OR (
                    CASE
                        WHEN death_month IN (1, 3, 5, 7, 8, 10, 12) THEN death_day <= 31
                        WHEN death_month IN (4, 6, 9, 11) THEN death_day <= 30
                        WHEN death_month = 2 THEN
                            death_day <= CASE
                                WHEN death_year IS NULL
                                     OR (death_year % 4 = 0 AND death_year % 100 <> 0)
                                     OR (death_year % 400 = 0)
                                THEN 29
                                ELSE 28
                            END
                        ELSE FALSE
                    END
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.speakers'::regclass
          AND conname = 'chk_speakers_death_date_after_birth_date'
    ) THEN
        ALTER TABLE public.speakers
            ADD CONSTRAINT chk_speakers_death_date_after_birth_date CHECK (
                birth_year IS NULL
                OR death_year IS NULL
                OR death_year > birth_year
                OR (
                    death_year = birth_year
                    AND (
                        birth_month IS NULL
                        OR death_month IS NULL
                        OR death_month > birth_month
                        OR (
                            death_month = birth_month
                            AND (
                                birth_day IS NULL
                                OR death_day IS NULL
                                OR death_day >= birth_day
                            )
                        )
                    )
                )
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_speakers_birthday
    ON public.speakers (birth_month, birth_day)
    WHERE birth_month IS NOT NULL AND birth_day IS NOT NULL;

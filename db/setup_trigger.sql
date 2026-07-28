-- 1. Create a function that emits a notification when a tick is inserted
CREATE OR REPLACE FUNCTION notify_new_tick()
RETURNS TRIGGER AS $$
BEGIN
  -- Send a notification via pg_notify to the 'new_tick' channel
  -- The payload is a compact JSON representation of the new tick row
  PERFORM pg_notify('new_tick', row_to_json(NEW)::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Create the trigger on the TICK_DATA table
-- This trigger calls the function above for every newly inserted row
DROP TRIGGER IF EXISTS tick_data_notify_trigger ON TICK_DATA;

CREATE TRIGGER tick_data_notify_trigger
AFTER INSERT ON TICK_DATA
FOR EACH ROW
EXECUTE FUNCTION notify_new_tick();

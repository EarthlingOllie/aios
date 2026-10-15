import { ScheduleList } from "@/components/schedules";

/** Every schedule the user has, the next to run first. */
export function SchedulerPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Scheduler</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Run agents and workflows on a schedule. Each time an agent's schedule fires, a new run
            of its current version starts with the schedule's message.
          </p>
        </header>
        <ScheduleList empty="Nothing is scheduled yet. Add a schedule to run an agent or a workflow automatically." />
      </div>
    </div>
  );
}

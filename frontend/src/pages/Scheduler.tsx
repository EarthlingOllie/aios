import { ScheduleList } from "@/components/schedules";

/** Every schedule and event trigger the organisation has, the next to run first. */
export function SchedulerPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6 md:px-8">
        <header>
          <h1 className="text-xl font-semibold">Scheduler</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Run agents and workflows on a schedule, or when something happens, such as a document
            being ingested. Each time an agent's schedule or trigger fires, a new run of its
            current version starts.
          </p>
        </header>
        <ScheduleList empty="Nothing is scheduled yet. Add a schedule or an event trigger to run an agent or a workflow automatically." />
      </div>
    </div>
  );
}

use super::SqliteStore;
use agent_api::{
    ApiError, ErrorCode, Job, JobRepository, JobResult, JobState, QueuedJob, Submission,
};
use rusqlite::{OptionalExtension, params};

fn internal<T>(_: T) -> ApiError {
    ApiError::internal()
}
impl JobRepository for SqliteStore {
    fn enqueue(&mut self, submission: &Submission, capacity: u32) -> agent_api::Result<Job> {
        submission.validate()?;
        let command = serde_json::to_string(&submission.command).map_err(internal)?;
        let tx = self.connection.transaction().map_err(internal)?;
        let existing: Option<(i64, String)> = tx
            .query_row(
                "SELECT id,command FROM api_jobs WHERE request_id=?1",
                [&submission.request_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(internal)?;
        if let Some((id, stored)) = existing {
            if stored != command {
                return Err(ApiError::new(
                    ErrorCode::Conflict,
                    "request_id already used for a different command",
                ));
            }
            tx.commit().map_err(internal)?;
            return self.job(id);
        }
        let count: u32 = tx
            .query_row(
                "SELECT COUNT(*) FROM api_jobs WHERE state IN ('queued','running')",
                [],
                |r| r.get(0),
            )
            .map_err(internal)?;
        if count >= capacity {
            return Err(ApiError::new(
                ErrorCode::QueueFull,
                "agent job queue is full",
            ));
        }
        tx.execute(
            "INSERT INTO api_jobs(request_id,command,state) VALUES(?1,?2,'queued')",
            params![submission.request_id, command],
        )
        .map_err(internal)?;
        let id = tx.last_insert_rowid();
        tx.commit().map_err(internal)?;
        self.job(id)
    }
    fn job(&self, id: i64) -> agent_api::Result<Job> {
        let row: Option<(String, String, Option<String>, Option<String>)> = self
            .connection
            .query_row(
                "SELECT request_id,state,result,error FROM api_jobs WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()
            .map_err(internal)?;
        let (request_id, state, result, error) =
            row.ok_or_else(|| ApiError::new(ErrorCode::NotFound, "job not found"))?;
        let state = match state.as_str() {
            "queued" => JobState::Queued,
            "running" => JobState::Running,
            "succeeded" => JobState::Succeeded,
            "failed" => JobState::Failed,
            _ => return Err(ApiError::internal()),
        };
        Ok(Job {
            id,
            request_id,
            state,
            result: result
                .map(|s| serde_json::from_str(&s).map_err(internal))
                .transpose()?,
            error: error
                .map(|s| serde_json::from_str(&s).map_err(internal))
                .transpose()?,
        })
    }
    fn claim_job(&mut self) -> agent_api::Result<Option<QueuedJob>> {
        let tx = self.connection.transaction().map_err(internal)?;
        let row: Option<(i64, String)> = tx
            .query_row(
                "SELECT id,command FROM api_jobs WHERE state='queued' ORDER BY id LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(internal)?;
        let Some((id, encoded)) = row else {
            return Ok(None);
        };
        let command = serde_json::from_str(&encoded).map_err(internal)?;
        tx.execute(
            "UPDATE api_jobs SET state='running',updated_at=CURRENT_TIMESTAMP WHERE id=?1",
            [id],
        )
        .map_err(internal)?;
        tx.commit().map_err(internal)?;
        Ok(Some(QueuedJob { id, command }))
    }
    fn finish_job(
        &mut self,
        id: i64,
        result: agent_api::Result<JobResult>,
    ) -> agent_api::Result<()> {
        let (state, result, error) = match result {
            Ok(result) => (
                "succeeded",
                Some(serde_json::to_string(&result).map_err(internal)?),
                None,
            ),
            Err(error) => (
                "failed",
                None,
                Some(serde_json::to_string(&error).map_err(internal)?),
            ),
        };
        let changed = self.connection.execute("UPDATE api_jobs SET state=?2,result=?3,error=?4,updated_at=CURRENT_TIMESTAMP WHERE id=?1 AND state='running'", params![id,state,result,error]).map_err(internal)?;
        if changed != 1 {
            return Err(ApiError::internal());
        }
        Ok(())
    }
    fn recover_jobs(&mut self) -> agent_api::Result<()> {
        self.connection.execute("UPDATE api_jobs SET state='queued',updated_at=CURRENT_TIMESTAMP WHERE state='running'", []).map_err(internal)?;
        Ok(())
    }
    fn pending_jobs(&self) -> agent_api::Result<u32> {
        self.connection
            .query_row(
                "SELECT COUNT(*) FROM api_jobs WHERE state IN ('queued','running')",
                [],
                |r| r.get(0),
            )
            .map_err(internal)
    }
}

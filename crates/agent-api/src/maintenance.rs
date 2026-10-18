//! Shared admission gate. A permit follows the actual write, including blocking work.
use crate::{ApiError, ErrorCode, Result};
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct State {
    draining: bool,
    writers: u32,
}
#[derive(Clone, Default)]
pub struct WriteGate(Arc<Mutex<State>>);
pub struct WritePermit(Option<Arc<Mutex<State>>>);
impl WritePermit {
    /// For embedded services that do not offer maintenance mode.
    pub fn untracked() -> Self {
        Self(None)
    }
}
impl Drop for WritePermit {
    fn drop(&mut self) {
        if let Some(state) = &self.0 {
            if let Ok(mut state) = state.lock() {
                state.writers -= 1;
            }
        }
    }
}
impl WriteGate {
    pub fn acquire(&self) -> Result<WritePermit> {
        let mut state = self.0.lock().map_err(|_| ApiError::internal())?;
        if state.draining {
            return Err(ApiError::new(
                ErrorCode::Unavailable,
                "node is draining for upgrade",
            ));
        }
        state.writers += 1;
        Ok(WritePermit(Some(self.0.clone())))
    }
    pub fn status(&self, draining: Option<bool>) -> Result<(bool, u32)> {
        let mut state = self.0.lock().map_err(|_| ApiError::internal())?;
        if let Some(value) = draining {
            state.draining = value;
        }
        Ok((state.draining, state.writers))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn draining_retains_existing_permits_and_rejects_new_writes() {
        let gate = WriteGate::default();
        let permit = gate.acquire().unwrap();
        assert_eq!(gate.status(Some(true)).unwrap(), (true, 1));
        assert!(gate.acquire().is_err());
        drop(permit);
        assert_eq!(gate.status(None).unwrap(), (true, 0));
        gate.status(Some(false)).unwrap();
        assert!(gate.acquire().is_ok());
    }
}

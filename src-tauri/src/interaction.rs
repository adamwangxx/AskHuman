//! Explicit host controls for interaction. This policy never manufactures a human response.
//! Codex hooks persist the verdict through `agents::delegated` because Codex filters MCP env.

use serde::{Deserialize, Serialize};

pub const DISABLED_MESSAGE: &str =
    "AskHuman interaction is disabled by the host environment or an interaction rule. \
Do not retry, escalate, or ask the human through another AskHuman entry point. \
Continue within the delegated task and return the result to the caller. \
This is not human approval or consent to end the caller's session.";

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Config {
    /// Any matching condition disables interaction. An empty list matches nothing.
    pub suppress_when: Vec<EnvCondition>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvCondition {
    #[serde(deserialize_with = "deserialize_env_name")]
    pub env: String,
    /// Omitted: nonempty value; supplied (including ""): exact, case-sensitive equality.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub equals: Option<String>,
}

fn deserialize_env_name<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<String, D::Error> {
    let name = String::deserialize(deserializer)?;
    let mut bytes = name.bytes();
    if !bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
        || !bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        return Err(serde::de::Error::custom(
            "env must be a nonempty environment variable name",
        ));
    }
    Ok(name)
}

impl Config {
    fn suppression_with(&self, read: impl Fn(&str) -> Option<String>) -> Option<&'static str> {
        if read("ASKHUMAN_INTERACTION").as_deref() == Some("off") {
            return Some("interaction_env_off");
        }
        self.suppress_when
            .iter()
            .any(|condition| {
                read(&condition.env).is_some_and(|value| match &condition.equals {
                    Some(expected) => value == *expected,
                    None => !value.is_empty(),
                })
            })
            .then_some("interaction_env_condition")
    }
}

/// Read current configuration on each call, without touching credentials. Never audit env values.
pub fn suppression() -> Option<&'static str> {
    // Keep the explicit override effective even when the config cannot be read.
    if std::env::var("ASKHUMAN_INTERACTION").ok().as_deref() == Some("off") {
        return Some("interaction_env_off");
    }
    crate::config::AppConfig::load_without_secrets()
        .interaction
        .suppression_with(|name| std::env::var(name).ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn verdict(config: &Config, vars: &[(&str, &str)]) -> Option<&'static str> {
        config.suppression_with(|name| {
            vars.iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| (*value).into())
        })
    }

    #[test]
    fn rules_are_exact_or_nonempty_and_match_any_without_host_specific_defaults() {
        let config: Config = serde_json::from_value(json!({"suppressWhen": [
            {"env": "LODY_OPENED_BY_SESSION_ID"}, {"env": "ROLE", "equals": "worker"},
            {"env": "EMPTY", "equals": ""}
        ]}))
        .unwrap();
        for vars in [
            vec![],
            vec![("LODY_SESSION_ID", "main")],
            vec![("LODY_OPENED_BY_SESSION_ID", "")],
            vec![("ROLE", "Worker")],
        ] {
            assert_eq!(verdict(&config, &vars), None, "{vars:?}");
        }
        for vars in [
            vec![("LODY_OPENED_BY_SESSION_ID", "parent")],
            vec![("ROLE", "worker")],
            vec![("EMPTY", "")],
        ] {
            assert_eq!(verdict(&config, &vars), Some("interaction_env_condition"));
        }
        assert_eq!(
            verdict(&Config::default(), &[("ASKHUMAN_INTERACTION", "off")]),
            Some("interaction_env_off")
        );
        assert_eq!(
            verdict(
                &config,
                &[("ASKHUMAN_INTERACTION", "on"), ("ROLE", "worker")]
            ),
            Some("interaction_env_condition")
        );
        assert_eq!(
            verdict(
                &Config::default(),
                &[("LODY_OPENED_BY_SESSION_ID", "parent")]
            ),
            None
        );
    }

    #[test]
    fn config_rejects_ambiguous_or_malformed_conditions() {
        for condition in [
            json!({}),
            json!({"env": ""}),
            json!({"env": "A=B"}),
            json!({"env": " ROLE"}),
            json!({"env": "ROLE", "equal": "worker"}),
        ] {
            assert!(serde_json::from_value::<EnvCondition>(condition).is_err());
        }
    }
}

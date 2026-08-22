-- Replace only the exact retired Market Masters seed configuration. Existing
-- profile versions remain immutable so sealed job manifests keep their
-- original provenance.

INSERT INTO discovery_profile_versions(profile_id, config_hash, created_at)
SELECT
    profile.id,
    '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677',
    strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z'
FROM discovery_profiles AS profile
JOIN analysis_subjects AS subject ON subject.id = profile.subject_id
JOIN discovery_profile_versions AS current_version
  ON current_version.id = profile.current_version_id
WHERE subject.canonical_name = '木野内栄治'
  AND subject.is_active = 1
  AND profile.is_active = 1
  AND current_version.config_hash =
      '70de980a44b8e6d51cf739f6dc3465ef3efb29f84fb31b4dac17b574553e7e68'
  AND (SELECT COUNT(*) FROM discovery_seed_channels
       WHERE profile_version_id = current_version.id) = 1
  AND EXISTS (
      SELECT 1 FROM discovery_seed_channels
      WHERE profile_version_id = current_version.id
        AND ordinal = 1
        AND youtube_channel_id = 'UCJ1DVBLVpe4FvBZZ94kreaQ'
  )
  AND (SELECT COUNT(*) FROM discovery_search_terms
       WHERE profile_version_id = current_version.id) = 1
  AND EXISTS (
      SELECT 1 FROM discovery_search_terms
      WHERE profile_version_id = current_version.id
        AND ordinal = 1
        AND search_term = '木野内栄治'
  )
  AND NOT EXISTS (
      SELECT 1 FROM discovery_profile_versions AS replacement
      WHERE replacement.profile_id = profile.id
        AND replacement.config_hash =
            '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677'
  );

INSERT INTO discovery_seed_channels(
    profile_version_id, ordinal, youtube_channel_id
)
SELECT replacement.id, 1, 'UCXvjRTXoDa8tKwdkTaukGug'
FROM discovery_profile_versions AS replacement
JOIN discovery_profiles AS profile ON profile.id = replacement.profile_id
JOIN analysis_subjects AS subject ON subject.id = profile.subject_id
WHERE subject.canonical_name = '木野内栄治'
  AND replacement.config_hash =
      '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677'
  AND NOT EXISTS (
      SELECT 1 FROM discovery_seed_channels
      WHERE profile_version_id = replacement.id
  );

INSERT INTO discovery_search_terms(profile_version_id, ordinal, search_term)
SELECT replacement.id, 1, '木野内栄治'
FROM discovery_profile_versions AS replacement
JOIN discovery_profiles AS profile ON profile.id = replacement.profile_id
JOIN analysis_subjects AS subject ON subject.id = profile.subject_id
WHERE subject.canonical_name = '木野内栄治'
  AND replacement.config_hash =
      '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677'
  AND NOT EXISTS (
      SELECT 1 FROM discovery_search_terms
      WHERE profile_version_id = replacement.id
  );

INSERT INTO audit_events(
    entity_type,
    entity_id,
    scope_id,
    operation,
    actor_kind,
    reason_code,
    reason_text,
    before_json,
    after_json,
    created_at
)
SELECT
    'discovery_profile',
    CAST(profile.id AS TEXT),
    NULL,
    'replace_version',
    'system',
    'DISCOVERY_PROFILE_SEED_REPLACED',
    'Replace retired Market Masters seed channel with its current public channel.',
    json_object(
        'config_hash', current_version.config_hash,
        'profile_id', profile.id,
        'profile_version_id', current_version.id,
        'subject_id', subject.id
    ),
    json_object(
        'config_hash', replacement.config_hash,
        'profile_id', profile.id,
        'profile_version_id', replacement.id,
        'subject_id', subject.id
    ),
    strftime('%Y-%m-%dT%H:%M:%f', 'now') || '000Z'
FROM discovery_profiles AS profile
JOIN analysis_subjects AS subject ON subject.id = profile.subject_id
JOIN discovery_profile_versions AS current_version
  ON current_version.id = profile.current_version_id
JOIN discovery_profile_versions AS replacement
  ON replacement.profile_id = profile.id
 AND replacement.config_hash =
     '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677'
WHERE subject.canonical_name = '木野内栄治'
  AND subject.is_active = 1
  AND profile.is_active = 1
  AND current_version.config_hash =
      '70de980a44b8e6d51cf739f6dc3465ef3efb29f84fb31b4dac17b574553e7e68'
  AND (SELECT COUNT(*) FROM discovery_seed_channels
       WHERE profile_version_id = current_version.id) = 1
  AND EXISTS (
      SELECT 1 FROM discovery_seed_channels
      WHERE profile_version_id = current_version.id
        AND ordinal = 1
        AND youtube_channel_id = 'UCJ1DVBLVpe4FvBZZ94kreaQ'
  )
  AND (SELECT COUNT(*) FROM discovery_search_terms
       WHERE profile_version_id = current_version.id) = 1
  AND EXISTS (
      SELECT 1 FROM discovery_search_terms
      WHERE profile_version_id = current_version.id
        AND ordinal = 1
        AND search_term = '木野内栄治'
  );

UPDATE discovery_profiles
SET current_version_id = (
    SELECT replacement.id
    FROM discovery_profile_versions AS replacement
    WHERE replacement.profile_id = discovery_profiles.id
      AND replacement.config_hash =
          '4ab6054ce98d57fa1e9a22a3e2dd6c4148132bba7c07ed9de7d0fa9a79da1677'
)
WHERE id IN (
    SELECT profile.id
    FROM discovery_profiles AS profile
    JOIN analysis_subjects AS subject ON subject.id = profile.subject_id
    JOIN discovery_profile_versions AS current_version
      ON current_version.id = profile.current_version_id
    WHERE subject.canonical_name = '木野内栄治'
      AND subject.is_active = 1
      AND profile.is_active = 1
      AND current_version.config_hash =
          '70de980a44b8e6d51cf739f6dc3465ef3efb29f84fb31b4dac17b574553e7e68'
      AND (SELECT COUNT(*) FROM discovery_seed_channels
           WHERE profile_version_id = current_version.id) = 1
      AND EXISTS (
          SELECT 1 FROM discovery_seed_channels
          WHERE profile_version_id = current_version.id
            AND ordinal = 1
            AND youtube_channel_id = 'UCJ1DVBLVpe4FvBZZ94kreaQ'
      )
      AND (SELECT COUNT(*) FROM discovery_search_terms
           WHERE profile_version_id = current_version.id) = 1
      AND EXISTS (
          SELECT 1 FROM discovery_search_terms
          WHERE profile_version_id = current_version.id
            AND ordinal = 1
            AND search_term = '木野内栄治'
      )
);

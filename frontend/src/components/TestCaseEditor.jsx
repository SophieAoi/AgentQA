import { useState } from "react";
import { createTestCase, updateTestCase } from "../api";

const ID_PATTERN = /^[A-Za-z0-9_-]+$/;

// Mirrors agent/runner.py::_VALID_TEST_CASE_ID_RE exactly — the backend is
// the real authority (it rejects an invalid id with a 400 regardless), but
// checking here too means a typo'd id is caught before a round trip, not
// after.
function validateId(id, isEditing) {
  if (isEditing) return null; // id is fixed once a case exists — not editable here.
  if (!id.trim()) return "Test case ID is required.";
  if (!ID_PATTERN.test(id)) return "Only letters, digits, underscores, and hyphens are allowed.";
  return null;
}

/**
 * Modal form for creating a new test case or editing an existing one.
 * `existing` is null for create, or a TestCase object for edit — the id
 * field locks once editing, since renaming is really delete+create under
 * a different filename, not a field on this form.
 */
export default function TestCaseEditor({ existing, suites, onClose, onSaved }) {
  const isEditing = Boolean(existing);
  const [id, setId] = useState(existing?.id ?? "");
  const [title, setTitle] = useState(existing?.title ?? "");
  const [description, setDescription] = useState(existing?.description?.trim() ?? "");
  const [suite, setSuite] = useState(existing?.suite ?? "");
  const [essential, setEssential] = useState(existing?.essential ?? false);
  const [preconditionsText, setPreconditionsText] = useState(
    (existing?.preconditions ?? []).join("\n")
  );
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    const idError = validateId(id, isEditing);
    if (idError) {
      setError(idError);
      return;
    }
    if (!title.trim()) {
      setError("Title is required.");
      return;
    }
    if (!description.trim()) {
      setError("Description is required.");
      return;
    }

    const preconditions = preconditionsText
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);

    const payload = {
      title: title.trim(),
      description: description.trim(),
      suite: suite.trim() || null,
      essential,
      preconditions,
    };

    setSaving(true);
    try {
      if (isEditing) {
        await updateTestCase(existing.id, payload);
      } else {
        await createTestCase({ id: id.trim(), ...payload });
      }
      onSaved();
    } catch (err) {
      setError(err.message);
      setSaving(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{isEditing ? `Edit ${existing.id}` : "New Test Case"}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <form className="modal-form" onSubmit={handleSubmit}>
          <label className="modal-field">
            <span className="modal-field-label">Test Case ID</span>
            <input
              type="text"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="e.g. TC_NEW_001"
              disabled={isEditing}
              autoFocus={!isEditing}
            />
            {!isEditing && (
              <span className="modal-field-hint">Letters, digits, underscores, and hyphens only.</span>
            )}
          </label>

          <label className="modal-field">
            <span className="modal-field-label">Title</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Short, specific summary"
              autoFocus={isEditing}
            />
          </label>

          <label className="modal-field">
            <span className="modal-field-label">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Step-by-step natural-language instructions for what the agent should do and verify."
              rows={7}
            />
          </label>

          <div className="modal-field-row">
            <label className="modal-field">
              <span className="modal-field-label">Suite</span>
              <input
                type="text"
                list="suite-options"
                value={suite}
                onChange={(e) => setSuite(e.target.value)}
                placeholder="e.g. Login"
              />
              <datalist id="suite-options">
                {suites.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            </label>

            <label className="modal-field modal-field--checkbox">
              <input type="checkbox" checked={essential} onChange={(e) => setEssential(e.target.checked)} />
              <span className="modal-field-label">Essential</span>
            </label>
          </div>

          <label className="modal-field">
            <span className="modal-field-label">
              Preconditions <span className="modal-field-hint-inline">(one per line, optional)</span>
            </span>
            <textarea
              value={preconditionsText}
              onChange={(e) => setPreconditionsText(e.target.value)}
              placeholder={'e.g. "requires login"'}
              rows={2}
            />
          </label>

          {error && <div className="modal-error">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="bulk-action-button bulk-action-button--ghost" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button type="submit" className="bulk-action-button bulk-action-button--primary" disabled={saving}>
              {saving ? "Saving..." : isEditing ? "Save Changes" : "Create Test Case"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

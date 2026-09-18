"""Low-level logic for the factory.

Part 2 of the spec: ``f_modules/`` is the only place low-level logic lives. Workflow scripts
(``factory/f_*.py``) stay thin — they hold a phase sequence and nothing else. Parsing,
subprocesses, retries, git mechanics and any reusable predicate belong here.

Module ownership is fixed by §10.5 and should not be improvised:

===================  ==========================================================
``data_types``       every schema: config, envelopes, phases, events, requests
``agents``           config load/validate, agent execution, parse-and-gate
``runner``           the ``Run`` object and the single phase primitive
``session``          id minting/joining, signal handling, ``Run`` construction
``agent_base``       the adapter protocol and lazy adapter lookup
``agent_*``          one coding-agent backend each
``gates``            reusable claim verification
``permissions``      snapshot / enforce / rollback
``quality``          deterministic command blocks and their envelope adapter
``changes``          git diff capture and its envelope adapter
``git_helper``       git plumbing only
``tracer``           file + database persistence
``console``          the single print-and-trace helper
``prompts``          render placeholders, save the audit copy
``utils``            ids, timestamps, operator env, prompt resolution
===================  ==========================================================
"""

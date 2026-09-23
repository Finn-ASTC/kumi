"""Explicit synthetic stop confirmation for bookkeeping tests, no actual transport."""
from unittest.mock import patch

import controls
import jobs
import watch


def stopped(index, state, token, evidence_path, now):
    """Exercise real control bookkeeping while replacing only terminal observation."""
    with patch.object(watch, 'observe', return_value={'state':'idle', 'screen':'Fixture stopped'}):
        inspected = controls.inspect(index, state['job_id'], now=now)
        state = jobs.change(index, state['job_id'], state['revision'], token, 'control-begin', {
            'kind':'cancel', 'inspection_path':inspected['inspection_path'],
            'method':{'kind':'manual','description':'Synthetic stop; fixture has no actual writer'},
            'evidence_path':str(evidence_path),'note':'Explicit test-only stop observation'}, now=now)
    return jobs.change(index, state['job_id'], state['revision'], token, 'control-receipt', {
        'control_id':state['control']['control_id'],'status':'confirmed','outcome':'stopped',
        'checks':{k:'clear' for k in controls.STOP_CHECKS}, 'observed_at':now,
        'evidence_path':str(evidence_path),'note':'Synthetic foreground/background/children/side-effect checks'}, now=now)

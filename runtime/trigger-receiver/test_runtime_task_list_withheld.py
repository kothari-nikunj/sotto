"""Withheld duplicate anchors are not a claim that the owner's task list is empty."""
from pathlib import Path
import copy
import pytest
from test_runtime_owner_loop_tool import context,call,request,rows,ke
from test_runtime_source_loop_tool import actual_writer_module
from test_runtime_loop_cleanup import prepare_cleanup
__all__=['context','actual_writer_module']

@pytest.mark.parametrize('scope',['open',None])
def test_all_duplicate_anchors_report_withheld_tasks_instead_of_empty_view(context,scope):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    for index,row in enumerate(rows()):
        duplicate=copy.deepcopy(row)
        duplicate['_path']=str(Path(cr._dir())/f'legacy-duplicate-{index}.md')
        cr._terminate(duplicate,'dismissed','merged_duplicate','2026-10-05')
        duplicate['merged_into']=duplicate['anchor_key'];cr._persist(duplicate)
    args={'operation':'propose_cleanup','source_revision':'a'*64}
    if scope:
        args['scope']=scope
    result=call(request(args,event='c'*64,operation='preview',text='Show my tasks'),context)
    assert result['status']=='preview'
    review=result['review'];assert review['rows']==[] and review['remaining']==4
    text=ke.render_cleanup_list(review)
    assert 'can be safely numbered' in text and '4 more' in text
    assert 'No open tasks' not in text and 'No stale loops were found' not in text

import assert from 'node:assert/strict';
import test from 'node:test';
import {calendarReviewHash, calendarReviewSchema, calendarReviewText, validCalendarReview, type CalendarPreview} from '../src/tools/owner-calendar-contract.js';
const revision='a'.repeat(64);
const common={calendar_id:'primary',event_id:revision,etag:'v1',summary:'Planning',html_link:'',source_revision:revision,notifications:'all',review_hash:revision};
function preview(raw: unknown): CalendarPreview {
  const review=calendarReviewSchema.parse(raw);
  review.review_hash=calendarReviewHash(review);
  return {version:1,status:'preview',event_id:revision,payload_hash:revision,review};
}
function create(start='2026-10-15T14:00:00-07:00',end='2026-10-15T14:30:00-07:00',fields={}) {
  return preview({operation:'create',calendar_id:'primary',event_id:revision,summary:'Planning',start,end,attendees:[],effective_attendees:[],organizer_email:null,organizer_added:false,location:'',description:'',notifications:'all',source_revision:revision,review_hash:revision,...fields});
}
test('calendar create review is concise without losing guests, agenda or invitations',()=>{
  const empty=create();
  assert.equal(calendarReviewText(empty),'I’ll add Planning to your main calendar.\nOct 15, 2026, 2:00 PM to 2:30 PM (UTC-07:00)\n\nCreate it?');
  assert.ok(validCalendarReview(empty));
  const full=create(undefined,undefined,{attendees:['guest@example.com'],effective_attendees:['guest@example.com','owner@example.com'],organizer_email:'owner@example.com',organizer_added:true,location:'Room 2',description:'First item\nSecond item'});
  const before=JSON.stringify(full),text=calendarReviewText(full);
  assert.match(text,/Guests: guest@example.com, owner@example.com\nLocation: Room 2\nAgenda:\nFirst item\nSecond item/);
  assert.match(text,/Create it and invite these guests\?/);
  assert.equal(JSON.stringify(full),before,'presentation cannot rewrite approval fields or hashes');
  const unknown=create(undefined,undefined,{attendees:['guest@example.com'],effective_attendees:['guest@example.com']});
  assert.match(calendarReviewText(unknown),/Your address is not known/);
});
test('calendar review preserves local midnight and year rather than converting offsets',()=>{
  assert.match(calendarReviewText(create('2026-12-31T23:30:00+05:45','2027-01-01T00:30:00+05:45')),/Dec 31, 2026, 11:30 PM \(UTC\+05:45\) to Jan 1, 2027, 12:30 AM \(UTC\+05:45\)/);
  assert.match(calendarReviewText(create('2026-10-15T00:00:00Z','2026-10-15T12:00:00Z')),/Oct 15, 2026, 12:00 AM to 12:00 PM \(UTC\)/);
});
test('calendar review retains differing DST offsets and exact sub-minute times',()=>{
  assert.match(calendarReviewText(create('2026-11-01T01:30:00-07:00','2026-11-01T01:30:00-08:00')),/Nov 1, 2026, 1:30 AM \(UTC-07:00\) to Nov 1, 2026, 1:30 AM \(UTC-08:00\)/);
  assert.match(calendarReviewText(create('2026-10-15T14:00:05.125-07:00','2026-10-15T14:30:00-07:00')),/2:00:05\.125 PM to 2:30 PM \(UTC-07:00\)/);
});
test('calendar move keeps both complete spans and notification warning',()=>{
  const p=preview({...common,operation:'move',calendar_id:'team@example.com',old_start:'2026-10-15T23:30:00-07:00',old_end:'2026-10-16T00:30:00-07:00',new_start:'2026-10-16T14:00:00-07:00',new_end:'2026-10-16T15:00:00-07:00'});
  const text=calendarReviewText(p);
  assert.match(text,/Calendar: team@example.com/);
  assert.match(text,/From: Oct 15, 2026, 11:30 PM \(UTC-07:00\) to Oct 16, 2026, 12:30 AM \(UTC-07:00\)/);
  assert.match(text,/To: Oct 16, 2026, 2:00 PM to 3:00 PM \(UTC-07:00\)/);
  assert.match(text,/Guests will be notified\. Move this meeting\?/);
  assert.ok(validCalendarReview(p));
});
test('calendar all-day series review keeps exclusive end, response and before/after clearing',()=>{
  const fields={...common,event_scope:'series',all_day:true,start:'2026-10-15',end:'2026-10-17',recurring_event_id:null,original_start:null};
  const update=preview({...fields,operation:'update',before_fields:{location:'Room 2'},fields:{location:''}});
  assert.match(calendarReviewText(update),/Applies to: entire recurring series\nWhen: All day, Oct 15, 2026; ends before Oct 17, 2026/);
  assert.match(calendarReviewText(update),/Location:\nBefore:\nRoom 2\nAfter:\n\(empty\)/);
  assert.match(calendarReviewText(update),/Guests will be notified\. Update the entire series\?/);
  const rsvp=preview({...fields,operation:'rsvp',self_email:'owner@example.com',old_response:'accepted',new_response:'tentative',old_comment:'Yes',new_comment:'May be late'});
  assert.match(calendarReviewText(rsvp),/When: All day, Oct 15, 2026; ends before Oct 17, 2026\nYour address: owner@example.com\nResponse: Accepted → Tentative/);
  assert.match(calendarReviewText(rsvp),/Your comment before: Yes\nYour comment after: May be late/);
  assert.match(calendarReviewText(rsvp),/The organizer and guests will be notified\. Mark tentative for the entire series\?/);
});

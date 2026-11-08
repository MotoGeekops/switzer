"""Regression tests exercise intent, real draft persistence, delivery, and providers."""
import asyncio
import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import discord
from switzer_ui import plain_text
import bot as bot_module
from ai_helper import AIHelperHub, install_ai_helper, parse_natural_task_request, parse_followup_ai_task
from chat_client import AIError, OpenRouterChat
from community_game import RuleError
from storage import StateStore
from test_community import defaults, interaction


class IntentTests(unittest.TestCase):
    def test_ordinary_questions_quotes_and_negations_never_schedule(self):
        for text in (
            'why does my PC crash every day?', 'I work out every 2 days, is that enough?',
            'Explain what remind me in 5 minutes means', 'Write about events in 5 minutes of the film',
            'Please do not remind me in 5 minutes', "Don't write an essay then in 5 minutes review it",
            'Never write an essay then in 5 minutes review it', 'How do I write an essay then in 5 minutes review it?',
            '"remind me in 5 minutes to study"', 'Explain "remind me in 5 minutes"',
            'The train arrives in 5 minutes then leaves', 'I send updates every day',
            'Explain why my PC crashes every day',
            'Write an essay about people who exercise every day',
            'Summarize what happens every day', 'Check why I wake up every hour',
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_natural_task_request(text))
                self.assertIsNone(parse_followup_ai_task(text))

    def test_polite_requests_and_durations(self):
        for text, seconds in (
            ('Can you please remind me in 5 minutes to study?', 300),
            ('remind me in an hour to check the oven', 3600),
            ('set a timer for half an hour', 1800), ('wake me in a minute', 60),
            ('in 10 seconds review my essay', 10), ('review my essay in 5 minutes', 300),
            ('remind me in 1.5 hours to study for 2 hours', 5400),
        ):
            with self.subTest(text=text):
                result = parse_natural_task_request(text)
                self.assertEqual(result['delay_seconds'], seconds)
        self.assertIsNone(parse_natural_task_request('write an essay in 1861 about Lincoln'))

    def test_unsupported_or_invalid_schedules_are_explicit(self):
        for text in ('remind me tomorrow at 9', 'remind me to study for 2 hours',
                     'remind me in 0 minutes', 'remind me in 999999999999999999999 days',
                     'post updates every day at 9 am', 'cancel my reminders except the oven one',
                     'cancel task #1234567', 'post updates every 5 seconds'):
            with self.subTest(text=text), self.assertRaises(RuleError):
                parse_natural_task_request(text)

    def test_multiple_followups_are_not_silently_merged(self):
        with self.assertRaises(RuleError):
            parse_followup_ai_task('Write an essay then in 5 minutes review it then in 10 minutes summarize it')


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.store = StateStore(self.path, defaults)
        self.now = 1760000000.0
        self.client = bot_module.MyBot()
        self.client._connection.user = SimpleNamespace(id=999, bot=True)
        self.client.ai = SimpleNamespace(complete=AsyncMock(return_value='Original draft. FINAL-PARAGRAPH'),
                                         search_web=AsyncMock(return_value=[]))
        self.helper = install_ai_helper(self.client, self.store)
        self.client.helper = self.helper
        self.helper.engine.clock = lambda: self.now
        self.channel = SimpleNamespace(id=100, guild=SimpleNamespace(id=1), send=AsyncMock())

    async def asyncTearDown(self):
        await self.helper.close()
        self.temp.cleanup()

    def message(self, text, uid=42, admin=False):
        return SimpleNamespace(id=123, author=SimpleNamespace(id=uid, bot=False, name='User',
            guild_permissions=SimpleNamespace(manage_guild=admin)), guild=SimpleNamespace(id=1),
            channel=self.channel, content='<@999> '+text, mentions=[self.client.user],
            attachments=[], reference=None, reply=AsyncMock())

    def task(self, **kwargs):
        return self.helper.engine.add_task(1, 42, 100, 'Review the draft', interval='once',
                                          delay_seconds=5, web_search=False, **kwargs)

    async def test_exact_user_request_persists_full_draft_across_restart(self):
        draft = 'ESSAY-START ' + 'original text ' * 450 + 'FINAL-PARAGRAPH'
        self.client.ai.complete.return_value = draft
        msg = self.message('create a essay for me about the american civil war than about 5 minutes review your essay and make sure its human like written. draft the essay now.')
        await self.helper.handle_message(msg)
        task = self.helper.engine.list_tasks(1)[0]
        self.assertEqual(task['next_run'], self.now+300)
        self.assertIn(draft, task['context_block'])
        sent = plain_text(*msg.reply.call_args.args, **msg.reply.call_args.kwargs) + ''.join(plain_text(*c.args, **c.kwargs) for c in self.channel.send.call_args_list)
        self.assertIn('Scheduled Follow-Up Task', sent)
        self.assertTrue(all(len(c.kwargs['embed']) <= 6000 for c in self.channel.send.call_args_list))
        reloaded = StateStore(self.path, defaults)
        helper = AIHelperHub(self.client, reloaded, clock=lambda: self.now)
        self.channel.history = lambda **kwargs: (_ for _ in ()).throw(AssertionError('Do not use unrelated channel history'))
        self.client.ai.complete.return_value = 'Reviewed the exact original essay.'
        self.now += 301
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await helper.tick(1)
        prompt = self.client.ai.complete.call_args.args[0][-1]['content']
        self.assertIn(draft, prompt)
        self.assertEqual(helper.engine.list_tasks(1), [])

    async def test_ai_failure_does_not_create_followup(self):
        self.client.ai.complete.side_effect = AIError('Service busy')
        msg = self.message('Write an essay then in 5 minutes review it')
        await self.helper.handle_message(msg)
        self.assertEqual(self.helper.engine.list_tasks(1), [])
        self.assertIn('No follow-up was scheduled', plain_text(*msg.reply.call_args.args, **msg.reply.call_args.kwargs))

    async def test_capacity_failure_is_reported(self):
        for i in range(25):
            self.task()
        msg = self.message('Write an essay then in 5 minutes review it')
        await self.helper.handle_message(msg)
        self.assertIn('No follow-up was scheduled', plain_text(*self.channel.send.call_args.args, **self.channel.send.call_args.kwargs))
        self.assertEqual(len(self.helper.engine.list_tasks(1)), 25)

    async def test_failed_initial_delivery_leaves_followup_paused(self):
        msg = self.message('Write an essay then in 5 minutes review it')
        msg.reply.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'No permission')
        with self.assertRaises(discord.Forbidden):
            await self.helper.handle_message(msg)
        self.assertEqual(self.helper.engine.list_tasks(1)[0]['status'], 'paused')

    async def test_failed_send_retries_without_regenerating_or_losing_task(self):
        task = self.task()
        self.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'retry')
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.helper.tick(1)
            self.assertEqual(task['runs'], 0)
            self.assertEqual(task['failures'], 1)
            self.channel.send.side_effect = None
            self.now = task['next_run'] + 1
            await self.helper.tick(1)
        self.client.ai.complete.assert_awaited_once()
        self.assertEqual(self.helper.engine.list_tasks(1), [])

    async def test_partial_long_delivery_resumes_at_unsent_chunk_after_restart(self):
        task = self.task()
        self.client.ai.complete.return_value = 'A' * 5000
        self.channel.send.side_effect = [None, discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'retry')]
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.helper.tick(1)
        self.assertEqual(task['sent_chunks'], 1)
        reloaded = StateStore(self.path, defaults)
        helper = AIHelperHub(self.client, reloaded, clock=lambda: self.now)
        self.now = task['next_run']+1
        self.channel.send.reset_mock(side_effect=True)
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await helper.tick(1)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIsNone(self.channel.send.call_args_list[0].kwargs.get('content'))
        self.client.ai.complete.assert_awaited_once()

    async def test_provider_failure_does_not_delete_task_or_starve_other_tasks(self):
        failed = self.task()
        good = self.task(direct_message='Reminder')
        self.client.ai.complete.side_effect = AIError('Rate limited')
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.helper.tick(1)
        self.assertEqual(self.helper.engine.list_tasks(1), [failed])
        self.assertEqual(failed['runs'], 0)
        self.assertIn('Rate limited', failed['last_error'])
        self.channel.send.assert_awaited_once()

    async def test_missing_cached_channel_is_fetched_and_failed_fetch_preserves_task(self):
        task = self.task(direct_message='Reminder')
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=None), patch.object(self.client, 'fetch_channel', new=AsyncMock(return_value=self.channel)) as fetch:
            await self.helper.tick(1)
        fetch.assert_awaited_once_with(100)
        self.assertEqual(self.helper.engine.list_tasks(1), [])
        task = self.task(direct_message='Reminder')
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=None), patch.object(self.client, 'fetch_channel', new=AsyncMock(side_effect=OSError('offline'))):
            await self.helper.tick(1)
        self.assertIn(task, self.helper.engine.list_tasks(1))

    async def test_concurrent_manual_and_scheduler_runs_cannot_duplicate_output(self):
        task = self.task()
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(*args, **kwargs):
            entered.set()
            await release.wait()
            return 'Done'
        self.client.ai.complete.side_effect = complete
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            running = asyncio.create_task(self.helper.execute_task(1, task))
            await entered.wait()
            with self.assertRaisesRegex(RuleError, 'already running'):
                await self.helper.execute_task(1, task)
            release.set()
            await running
        self.channel.send.assert_awaited_once()

    async def test_cancel_during_generation_prevents_delivery(self):
        task = self.task()
        async def complete(*args, **kwargs):
            self.helper.engine.remove_task(1, task['id'])
            return 'Cancelled output'
        self.client.ai.complete.side_effect = complete
        self.now += 6
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.helper.tick(1)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.helper.engine.list_tasks(1), [])

    async def test_only_creator_is_pinged_even_if_ai_returns_other_mentions(self):
        self.client.ai.complete.return_value = '<@43> @everyone <@&77>'
        task = self.task()
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.helper.execute_task(1, task)
        mentions = self.channel.send.call_args.kwargs['allowed_mentions'].to_dict()
        self.assertEqual(mentions['users'], [42])
        self.assertNotIn('everyone', mentions['parse'])
        self.assertNotIn('roles', mentions['parse'])

    async def test_recurring_chat_requires_admin_and_reminder_bypasses_ai(self):
        msg = self.message('remind me every 2 hours to drink water')
        await self.helper.handle_message(msg)
        self.assertEqual(self.helper.engine.list_tasks(1), [])
        self.assertIn('Manage Server', plain_text(*msg.reply.call_args.args, **msg.reply.call_args.kwargs))
        msg = self.message('remind me every 2 hours to drink water', admin=True)
        await self.helper.handle_message(msg)
        task = self.helper.engine.list_tasks(1)[0]
        self.assertEqual(task['repeat_seconds'], 7200)
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            self.now += 7201
            await self.helper.tick(1)
        self.client.ai.complete.assert_not_awaited()
        self.assertIn('drink water', plain_text(*self.channel.send.call_args.args, **self.channel.send.call_args.kwargs))
        self.assertEqual(task['next_run'], self.now+7200)

    async def test_members_can_manage_own_tasks_but_not_another_users(self):
        task = self.task()
        cmd = self.client.tree.get_command('task')
        own = interaction(uid=42)
        for action in ('pause', 'resume'):
            await cmd.callback(own, action=action, task_id=task['id'])
        other = interaction(uid=43)
        with self.assertRaises(RuleError):
            await cmd.callback(other, action='remove', task_id=task['id'])
        await cmd.callback(own, action='remove', task_id=task['id'])
        self.assertEqual(self.helper.engine.list_tasks(1), [])

    async def test_username_cannot_grant_admin_access(self):
        member = interaction()
        member.user.name = 'lovington.'
        for name, kwargs in (('task', dict(action='add', prompt='news', interval='1h')),
                             ('kb', dict(action='clear')), ('autowatch', dict(mode='all'))):
            with self.subTest(command=name), self.assertRaises(RuleError):
                await self.client.tree.get_command(name).callback(member, **kwargs)

    async def test_uncached_reply_to_bot_includes_full_reference(self):
        msg = self.message('Review this draft')
        msg.content = 'Review this draft'
        msg.mentions = []
        msg.reference = SimpleNamespace(resolved=None, message_id=888)
        self.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(author=self.client.user, content='Referenced full draft FINAL', attachments=[]))
        await self.helper.handle_message(msg)
        self.assertIn('Referenced full draft FINAL', self.client.ai.complete.call_args.args[0][-1]['content'])

    async def test_slash_ask_uses_real_task_engine(self):
        inter = interaction(uid=42)
        inter.id, inter.guild, inter.channel = 123, SimpleNamespace(id=1), self.channel
        inter.user.bot = False
        inter.followup.send = AsyncMock()
        with patch.object(bot_module, 'bot', self.client):
            await bot_module.ask.callback(inter, message='remind me in 10 seconds to study')
        self.assertEqual(len(self.helper.engine.list_tasks(1)), 1)
        self.assertIn('Reminder locked in', plain_text(*inter.followup.send.call_args.args, **inter.followup.send.call_args.kwargs))
        self.client.ai.complete.assert_not_awaited()

    async def test_empty_search_results_are_not_presented_as_verified_facts(self):
        await self.helper.answer(1, 42, 'latest news today')
        self.assertIn('could not verify current facts', self.client.ai.complete.call_args.args[0][-1]['content'])

    async def test_legacy_partial_text_delivery_preserves_old_chunk_boundaries(self):
        task = self.task()
        old_text = f"⚡ **Switzer Scheduled Task (`#{task['id']}`)** · <@42>\n" + 'A'*2000 + 'UNSENT-END-' + 'B'*3000
        task['pending_text'] = old_text
        task['sent_chunks'] = 1
        self.store.mark_dirty()
        await self.store.flush()
        helper = AIHelperHub(self.client, StateStore(self.path, defaults), clock=lambda: self.now)
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await helper.execute_task(1, helper.engine.list_tasks(1)[0])
        self.assertEqual(self.channel.send.await_count, 2)
        delivered = ''.join(call.kwargs['embed'].description for call in self.channel.send.call_args_list)
        self.assertEqual(delivered, old_text[1990:])
        self.client.ai.complete.assert_not_awaited()
        self.assertTrue(all(call.kwargs['content'] is None for call in self.channel.send.call_args_list))

    async def test_replied_to_embed_is_available_to_ai(self):
        import discord
        msg = self.message('Review this draft')
        msg.reference = SimpleNamespace(resolved=SimpleNamespace(
            author=self.client.user, content='', attachments=[],
            embeds=[discord.Embed(description='Original embedded essay FINAL-PARAGRAPH')]))
        await self.helper.handle_message(msg)
        prompt = self.client.ai.complete.call_args.args[0][-1]['content']
        self.assertIn('Original embedded essay FINAL-PARAGRAPH', prompt)

    async def test_followup_notice_failure_keeps_task_paused(self):
        msg = self.message('Write an essay then in 5 minutes review it')
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing permission')
        with self.assertRaises(discord.Forbidden):
            await self.helper.handle_message(msg)
        msg.reply.assert_awaited_once()
        self.assertEqual(self.helper.engine.list_tasks(1)[0]['status'], 'paused')

    async def test_normal_slash_ask_renders_cards_without_pinging(self):
        inter = interaction(uid=42)
        with patch.object(bot_module, 'bot', self.client), patch.object(bot_module, 'ask_openrouter', new=AsyncMock(return_value='# Answer\n\nUseful response')):
            await bot_module.ask.callback(inter, message='Explain photosynthesis')
        card = inter.followup.send.call_args.kwargs['embed']
        self.assertEqual(card.description, '**Answer**\n\nUseful response')
        self.assertEqual(inter.followup.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_repeated_failures_pause_task_and_resume_clears_retry_count(self):
        task = self.task()
        self.client.ai.complete.side_effect = AIError('Rate limited')
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            for _ in range(5):
                self.now = task['next_run'] + 1
                await self.helper.tick(1)
        self.assertEqual(task['status'], 'paused')
        self.assertEqual(task['runs'], 0)
        inter = interaction(uid=42)
        await self.client.tree.get_command('task').callback(inter, action='resume', task_id=task['id'])
        self.assertEqual(task['failures'], 0)
        self.assertEqual(task['status'], 'active')

    async def test_large_task_and_kb_lists_stay_inside_discord_limits(self):
        for _ in range(25):
            task = self.task()
            task['prompt'] = 'P' * 1500
            task['last_error'] = 'E' * 300
        inter = interaction()
        await self.client.tree.get_command('task').callback(inter, action='list')
        self.assertGreater(len(inter.followup.send.call_args_list), 0)
        for args, kwargs in inter.response.calls + inter.followup.send.call_args_list:
            self.assertLessEqual(len(kwargs['embed']), 6000)
        for i in range(30):
            self.helper.engine.add_kb(1, str(i), 'K' * 1000)
        inter = interaction()
        await self.client.tree.get_command('kb').callback(inter, action='list')
        self.assertGreater(len(inter.response.calls + inter.followup.send.call_args_list), 1)
        for args, kwargs in inter.response.calls + inter.followup.send.call_args_list:
            self.assertLessEqual(len(kwargs['embed']), 6000)

    async def test_running_scheduler_delivers_reminder_while_ai_task_is_blocked(self):
        self.task()
        self.task(direct_message='URGENT TIMER')
        self.now += 6
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client.ai.complete.side_effect = blocked
        with patch.object(self.client, 'wait_until_ready', new=AsyncMock()), \
             patch.object(self.client, 'is_closed', return_value=False), \
             patch.object(self.client, 'get_guild', return_value=SimpleNamespace(id=1)), \
             patch.object(self.client, 'get_channel', return_value=self.channel):
            self.helper.start()
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(100):
                if self.channel.send.await_count:
                    break
                await asyncio.sleep(0.005)
            self.assertIn('URGENT TIMER', plain_text(*self.channel.send.call_args.args, **self.channel.send.call_args.kwargs))
            # A send starting does not mean its receipt has been saved yet.
            # Wait for delivery completion before testing shutdown of the blocked AI task.
            async def reminder_completed():
                while len(self.helper.engine.list_tasks(1)) != 1:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(reminder_completed(), 2)
            await self.helper.close()
        self.assertFalse(self.helper.background_tasks)
        self.assertFalse(self.helper.running_tasks)
        self.assertEqual(len(self.helper.engine.list_tasks(1)), 1)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def session(self, responses):
        captured = []
        class Request:
            async def __aenter__(self):
                return responses.pop(0)
            async def __aexit__(self, *args):
                return False
        def post(url, **kwargs):
            captured.append((url, kwargs))
            return Request()
        return SimpleNamespace(post=post), captured

    def response(self, data, status=200):
        return SimpleNamespace(status=status, json=AsyncMock(return_value=data))

    async def test_gemini_roles_system_and_api_key_header(self):
        session, calls = self.session([self.response({'candidates':[{'content':{'parts':[{'text':'hidden','thought':True},{'text':'Answer'}]}}]})])
        reply = await OpenRouterChat(session, None, 'fake-secret').complete([
            {'role':'system','content':'Be accurate'}, {'role':'user','content':'First'},
            {'role':'user','content':'Second'}, {'role':'assistant','content':'Earlier'}, {'role':'user','content':'Next'}])
        self.assertEqual(reply, 'Answer')
        url, kwargs = calls[0]
        self.assertNotIn('fake-secret', url)
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'fake-secret')
        payload = kwargs['json']
        self.assertEqual([m['role'] for m in payload['contents']], ['user','model','user'])
        self.assertEqual(len(payload['contents'][0]['parts']), 2)
        self.assertEqual(payload['systemInstruction']['parts'], [{'text':'Be accurate'}])

    async def test_empty_blocked_or_malformed_gemini_falls_back(self):
        for data in ({}, {'candidates':[]}, {'candidates':None}, {'candidates':[{'content':None}]}, []):
            with self.subTest(data=data):
                session, calls = self.session([self.response(data),self.response({'choices':[{'message':{'content':'Fallback answer'}}]})])
                self.assertEqual(await OpenRouterChat(session, 'key', 'gemini').complete([{'role':'user','content':'hi'}]), 'Fallback answer')
                self.assertEqual(len(calls), 2)

    async def test_gemini_only_failure_has_no_unauthenticated_fallback(self):
        session, calls = self.session([self.response({}, 429)])
        with self.assertRaises(AIError):
            await OpenRouterChat(session, None, 'gemini').complete([{'role':'user','content':'hi'}])
        self.assertEqual(len(calls), 1)

    async def test_gemini_vision_preserves_actual_image_bytes(self):
        session, calls = self.session([self.response({'candidates':[{'content':{'parts':[{'text':'Blue image'}]}}]})])
        client = OpenRouterChat(session, None, 'gemini')
        image = {'inlineData':{'mimeType':'image/png','data':base64.b64encode(b'actual-image-bytes').decode()}}
        with patch.object(client, '_image_part', new=AsyncMock(return_value=image)) as fetch:
            answer = await client.complete([{'role':'user','content':[{'type':'text','text':'Describe it'}, {'type':'image_url','image_url':{'url':'https://cdn.discordapp.com/attachments/test.png'}}]}])
        self.assertEqual(answer, 'Blue image')
        self.assertEqual(calls[0][1]['json']['contents'][0]['parts'][1], image)
        fetch.assert_awaited_once()

    async def test_image_download_rejects_non_discord_urls(self):
        client = OpenRouterChat(SimpleNamespace(), None, 'gemini')
        for url in ('http://127.0.0.1/a.png', 'https://cdn.discordapp.com.evil.example/a.png', 'file:///secret', 'https://cdn.discordapp.com:9000/a.png'):
            with self.subTest(url=url), self.assertRaises(AIError):
                await client._image_part(url)

    async def test_closed_session_is_friendly(self):
        with self.assertRaisesRegex(AIError, 'restarting'):
            await OpenRouterChat(SimpleNamespace(closed=True), 'key').complete([])


if __name__ == '__main__':
    unittest.main()

import base64
import copy
import unittest
from unittest.mock import patch

from nacl.public import PrivateKey, SealedBox

from github_state import ConflictError, GitHubState, STATE_FILE, bind_account, encode, overview, seal, unseal
from github_worker import control, control_operation, qr_binding, register_client


class MemoryAPI:
    def __init__(self, key):
        self.files={'control.json':{'version':1,'public_key':base64.b64encode(bytes(key.public_key)).decode()}}
        self.revisions={'control.json':'1'}
        self.counter=1
        self.conflict_once=False

    def get_file(self,path):
        return copy.deepcopy(self.files.get(path)),self.revisions.get(path)

    def put_file(self,path,value,sha=None):
        if self.conflict_once and path==STATE_FILE:
            self.conflict_once=False
            raise ConflictError()
        if sha != self.revisions.get(path):raise ConflictError()
        self.counter+=1
        self.files[path]=copy.deepcopy(value)
        self.revisions[path]=str(self.counter)


class GitHubCase(unittest.TestCase):
    def setUp(self):
        self.key=PrivateKey.generate();self.api=MemoryAPI(self.key)
        self.store=GitHubState(self.api,base64.b64encode(bytes(self.key)).decode())
        self.account={'cookies':{'wr_vid':'123','wr_skey':'COOKIE-SECRET'},'headers':{'user-agent':'test'}}

    def test_state_is_encrypted_and_sealed_commands_roundtrip(self):
        self.store.mutate(lambda s:bind_account(s,self.account,{'name':'我的账户','email':'me@example.com','read_num':40}))
        serialized=encode(self.api.files)
        self.assertNotIn(b'COOKIE-SECRET',serialized)
        self.assertNotIn(b'me@example.com',serialized)
        state,_=self.store.read()
        self.assertEqual(state['accounts'][0]['cookies']['wr_skey'],'COOKIE-SECRET')
        public=base64.b64encode(bytes(self.key.public_key)).decode()
        self.assertEqual(unseal(seal({'curl':'private curl'},public),self.key),{'curl':'private curl'})

    def test_overview_never_exposes_password_or_cookies(self):
        state=self.store.mutate(lambda s:bind_account(s,self.account,{'read_num':40}),publish=False)
        state['email']['password']='SMTP-SECRET'
        public=overview(state)
        self.assertNotIn('COOKIE-SECRET',str(public))
        self.assertNotIn('SMTP-SECRET',str(public))
        self.assertTrue(public['email']['has_password'])
        self.assertEqual(len(public['statistics']['days']),7)

    def test_only_intended_browser_can_read_response(self):
        client=PrivateKey.generate();public=base64.b64encode(bytes(client.public_key)).decode()
        self.store.respond('a'*32,public,{'qr':'QR-SECRET'})
        value=self.api.files['responses/'+'a'*32+'.json']['ciphertext']
        self.assertEqual(unseal(value,client),{'qr':'QR-SECRET'})
        with self.assertRaises(Exception):unseal(value,PrivateKey.generate())
        self.assertNotIn(b'QR-SECRET',encode(self.api.files))

    def test_qr_worker_publishes_changed_qr_without_status_change(self):
        class Manager:
            closed = False

            def __init__(self, local):
                self.local = local

            def start(self, options):
                account = self.local.bind({'wr_vid':'test','wr_skey':'test-cookie'}, {}, read_num=2)
                self.states = iter([
                    {'status':'starting','qr':None},
                    {'status':'waiting','qr':'first','qr_version':1},
                    {'status':'waiting','qr':'first','qr_version':1},
                    {'status':'waiting','qr':'second','qr_version':2},
                    {'status':'success','account':account},
                ])
                return 'local-session'

            def state(self, _):
                return next(self.states)

            def close(self):
                Manager.closed = True

        client = PrivateKey.generate()
        public = base64.b64encode(bytes(client.public_key)).decode()
        with patch('github_worker.QRLoginManager', Manager), patch('github_worker.time.sleep'), \
             patch.object(self.store, 'respond', wraps=self.store.respond) as responses:
            qr_binding(self.store, 'c'*32, public, {'read_num':2})
        waiting = [call.args[2] for call in responses.call_args_list if call.args[2]['status']=='waiting']
        self.assertEqual([r['qr'] for r in waiting], ['first','second'])
        self.assertTrue(all('expires_at' not in r for r in waiting))
        self.assertTrue(Manager.closed)
        response = self.api.files['responses/'+'c'*32+'.json']['ciphertext']
        self.assertEqual(unseal(response, client)['status'], 'success')

    def test_rebind_preserves_settings_and_rejects_other_account(self):
        state=self.store.defaults()
        account_id=bind_account(state,self.account,{'name':'账户','read_num':20})
        state['accounts'][0]['enabled']=False
        old_version=state['accounts'][0]['session_version']
        bind_account(state,{'cookies':{'wr_vid':'123','wr_skey':'new'},'headers':{}},{'account_id':account_id})
        self.assertEqual(len(state['accounts']),1)
        self.assertEqual(state['accounts'][0]['read_num'],20)
        self.assertFalse(state['accounts'][0]['enabled'])
        self.assertNotEqual(state['accounts'][0]['session_version'],old_version)
        with self.assertRaises(ValueError):bind_account(state,{'cookies':{'wr_vid':'456','wr_skey':'new'},'headers':{}},{'account_id':account_id})

    def test_optimistic_retry_does_not_duplicate_imports(self):
        self.api.conflict_once=True
        with patch('github_state.time.sleep'):
            self.store.mutate(lambda s:bind_account(s,self.account,{'read_num':40}))
        state,_=self.store.read();self.assertEqual(len(state['accounts']),1)

    def test_configuration_and_client_publication(self):
        client=PrivateKey.generate();public=base64.b64encode(bytes(client.public_key)).decode()
        def apply(state):
            register_client(state,'b'*32,public)
            bind_account(state,self.account,{'read_num':40})
            control_operation(state,'schedule',{'enabled':False})
            control_operation(state,'email',{'enabled':True,'host':'smtp.example.com','port':465,'security':'ssl','username':'me@example.com','password':'secret','sender':'me@example.com','recipient':'reader@example.com'})
        self.store.mutate(apply)
        value=self.api.files['responses/'+'b'*32+'.json']['ciphertext']
        snapshot=unseal(value,client)['overview']
        self.assertIsNone(snapshot['next_run'])
        self.assertTrue(snapshot['email']['has_password'])
        self.assertNotIn('password',snapshot['email'])

    def test_curl_import_update_and_delete(self):
        state=self.store.defaults()
        control_operation(state,'import',{'curl':"curl https://weread.qq.com -b 'wr_vid=1; wr_skey=secret'",'name':'我','read_num':4})
        account_id=state['accounts'][0]['id']
        control_operation(state,'update',{'account_id':account_id,'enabled':False,'name':'新的名字','read_num':8})
        self.assertFalse(state['accounts'][0]['enabled'])
        self.assertEqual(state['accounts'][0]['read_num'],8)
        with self.assertRaises(ValueError):control_operation(state,'update',{'account_id':account_id,'read_num':0})
        control_operation(state,'delete',{'account_id':account_id})
        self.assertEqual(state['accounts'],[])

    def test_notifications_are_encrypted_and_credentials_are_never_returned(self):
        def apply(state):
            control_operation(state,'notifications',{'channel':'pushplus','settings':{'enabled':True,'token':'PRIVATE-PUSH-TOKEN'}})
            control_operation(state,'notifications',{'channel':'webhook','settings':{'enabled':True,'format':'feishu',
                'url':'https://example.com/PRIVATE-HOOK','secret':'PRIVATE-SIGN-KEY','headers':'{"Authorization":"PRIVATE-HEADER"}'}})
        self.store.mutate(apply)
        state,_=self.store.read()
        public=overview(state)
        self.assertTrue(public['notifications']['pushplus']['has_token'])
        self.assertTrue(public['notifications']['webhook']['has_secret'])
        for token in ['PRIVATE-PUSH-TOKEN','PRIVATE-HOOK','PRIVATE-SIGN-KEY','PRIVATE-HEADER']:
            self.assertNotIn(token,str(public))
            self.assertNotIn(token.encode(),encode(self.api.files))
        control_operation(state,'notifications',{'channel':'pushplus','settings':{'enabled':False,'token':''}})
        self.assertEqual(state['notifications']['pushplus']['token'],'PRIVATE-PUSH-TOKEN')

    def test_existing_encrypted_state_imports_legacy_notification_secrets(self):
        import os
        old=self.store.defaults();old.pop('notifications')
        self.api.files[STATE_FILE]={'version':1,'ciphertext':base64.b64encode(self.store.box.encrypt(encode(old))).decode()}
        self.api.revisions[STATE_FILE]='old'
        with patch.dict(os.environ,{'PUSH_METHOD':'pushplus','PUSHPLUS_TOKEN':'IMPORTED-TOKEN'}):
            state,_=self.store.read()
            self.assertTrue(state['notifications']['pushplus']['enabled'])
            self.assertEqual(state['notifications']['pushplus']['token'],'IMPORTED-TOKEN')
            self.store.mutate(lambda s:control_operation(s,'notifications',{'channel':'pushplus','settings':{'enabled':False}}))
            persisted,_=self.store.read()
            self.assertFalse(persisted['notifications']['pushplus']['enabled'])

    def test_actions_notification_test_uses_encrypted_command_without_saving_test_values(self):
        import os
        self.store.mutate(lambda state:control_operation(state,'notifications',{'channel':'webhook',
            'settings':{'enabled':True,'format':'feishu','url':'https://example.com/SAVED-HOOK','secret':'SAVED-SIGN'}}))
        client=PrivateKey.generate()
        public=base64.b64encode(bytes(client.public_key)).decode()
        payload=seal({'channel':'webhook','settings':{'enabled':False,'url':'https://example.com/TEST-HOOK','secret':''}},
                     base64.b64encode(bytes(self.key.public_key)).decode())
        environment={'CONTROL_SESSION':'d'*32,'CONTROL_PUBLIC_KEY':public,
                     'CONTROL_OPERATION':'test-notification','CONTROL_PAYLOAD':payload}
        with patch.dict(os.environ,environment), patch('github_worker.GitHubState',return_value=self.store), \
             patch('github_worker.send_channel',return_value=True) as send:
            self.assertEqual(control(),0)
        self.assertEqual(send.call_args.args[1]['url'],'https://example.com/TEST-HOOK')
        self.assertEqual(send.call_args.args[1]['secret'],'SAVED-SIGN')
        self.assertTrue(send.call_args.kwargs['test'])
        state,_=self.store.read()
        self.assertEqual(state['notifications']['webhook']['url'],'https://example.com/SAVED-HOOK')
        response=self.api.files['responses/'+'d'*32+'.json']['ciphertext']
        self.assertEqual(unseal(response,client)['status'],'success')


if __name__=='__main__':unittest.main()

import base64
import copy
import unittest
from unittest.mock import patch

from nacl.public import PrivateKey, SealedBox

from github_state import ConflictError, GitHubState, STATE_FILE, bind_account, encode, overview, seal, unseal
from github_worker import control_operation, register_client


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


if __name__=='__main__':unittest.main()

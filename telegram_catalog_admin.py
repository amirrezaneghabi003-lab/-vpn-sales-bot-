"""In-chat catalogue management for Telegram admins, explicit two-step confirmation."""
from __future__ import annotations

import json
import secrets
from service_catalog import CatalogStore, CatalogError, FIELDS
from customer_experience import money


def esc(s):
    import html
    return html.escape(str(s or ''),quote=False)


def kb(*rows):
    return {'inline_keyboard': [[{'text':name,'callback_data':action} for name,action in row] for row in rows]}


class CatalogAdminMixin:
    def catalog_db(self):
        return CatalogStore(self.db.path)

    def catalog_list(self, admin, offset=0, msg=None):
        if not self.is_admin(admin):return False
        offset=max(0,min(int(offset),10000))
        plans=self.catalog_db().all(include_archived=True)
        rows=[]
        for x in plans[offset:offset+12]:
            icon='🗄' if x['archived'] else ('✅' if x['available'] else '⛔')
            rows.append([(f'{icon} {x["display"][:32]} · {money(x["price"])}',f'adm:catview:{x["plan_id"]}')])
        navigation=[]
        if offset: navigation.append(('◀️ قبلی',f'adm:catalog:{max(0,offset-12)}'))
        if offset+12<len(plans):navigation.append(('بعدی ▶️',f'adm:catalog:{offset+12}'))
        if navigation:rows.append(navigation)
        rows += [[('➕ افزودن سرویس جدید','adm:catnew')],
                 [('🗂 مدیریت دسته‌ها','adm:ctlist')], [('🔙 مدیریت','adm:home')]]
        self.screen(admin,f'📦 <b>مدیریت سرویس‌ها</b>\n\n{len(plans)} سرویس ثبت شده است.\n'
                    'برای تغییر قیمت، موجودی یا مشخصات، سرویس را انتخاب کنید.',kb(*rows),msg)
        return True

    def catalog_view(self,admin,pid,msg=None):
        if not self.is_admin(admin):return False
        x=self.catalog_db().get(pid)
        if not x:return self.safe_send(admin,'سرویس پیدا نشد.')
        active='🗄 آرشیوشده' if x['archived'] else ('✅ موجود' if x['available'] else '⛔ ناموجود')
        text=(f'📦 <b>{esc(x["display"])}</b>\n\n'
              f'شناسه: <code>{esc(pid)}</code>\n'
              f'دسته: {esc(x["type"])}\n'
              f'حجم: {x["size"]//1024} GB\n'
              f'قیمت: <b>{money(x["price"])} تومان</b>\n'
              f'کاربران: {esc(x["users"])}\n'
              f'اعتبار: {esc(x["duration"] or "—")}\n'
              f'وضعیت: {active}\n'
              f'ویرایش نسخه: <code>{x["revision"]}</code>')
        rows=[]
        if not x['archived']:
            rows += [[('💰 قیمت',f'adm:catedit:{pid}:price'),('✏️ عنوان',f'adm:catedit:{pid}:display')],
                     [('📦 حجم',f'adm:catedit:{pid}:size')] + ([] if x['type']=='تک کاربره نامحدود' else [('👥 کاربران',f'adm:catedit:{pid}:users')]),
                     [('📅 اعتبار',f'adm:catedit:{pid}:duration'),('🗂 دسته',f'adm:catedit:{pid}:type')],
                     [('⛔ ناموجود کردن' if x['available'] else '✅ موجود کردن',f'adm:catask:{pid}:available')]]
        if pid.startswith('CUSTOM_'):
            rows.append([('♻️ بازگردانی از آرشیو' if x['archived'] else '🗄 آرشیو امن سرویس',f'adm:catask:{pid}:archived')])
        if not x['deleted']:
            rows += [[('🗑 حذف کامل از فروشگاه',f'adm:catdelask:{pid}')]]
        rows += [[('🔙 فهرست سرویس‌ها','adm:catalog')]]
        self.screen(admin,text,kb(*rows),msg)
        return True

    def _ask(self,admin,text,state,context,msg=None):
        self.db.set_state(admin,state,json.dumps(context,ensure_ascii=False))
        self.screen(admin,text+'\n\nبرای لغو /cancel را ارسال کنید.',kb([('🔙 بازگشت','adm:catalog')]),msg)
        return True

    def catalog_new(self,admin,msg=None):
        if not self.is_admin(admin):return False
        return self._ask(admin,'➕ <b>سرویس جدید</b>\nنام نمایشی بسته را وارد کنید (3 تا 80 نویسه).',
                         'catalog_new',{'step':'display','data':{},'nonce':secrets.token_hex(12)},msg)

    def catalog_text(self,admin,content):
        if not self.is_admin(admin):return False
        state,raw=self.db.get_state(admin)
        if state=='catalog_category_new':
            try:
                cid=self.catalog_db().create_category(admin,content)
                self.db.clear_state(admin)
                return self.category_view(admin,cid)
            except CatalogError as exc:
                return self.safe_send(admin,'⚠️ '+str(exc)+'\nنام دیگری بفرستید یا /cancel را بزنید.')
        if state not in ('catalog_new','catalog_edit'):return False
        try:data=json.loads(raw)
        except (TypeError,ValueError):
            self.db.clear_state(admin)
            return self.safe_send(admin,'اطلاعات مرحله ناقص بود؛ دوباره از مدیریت سرویس‌ها شروع کنید.')
        if state=='catalog_edit':
            try:value=self._validated(data['field'],content)
            except CatalogError as exc:return self.safe_send(admin,'⚠️ '+str(exc)+'\nلطفاً مقدار دیگری بفرستید.')
            data['value']=value
            self.db.set_state(admin,'catalog_confirm',json.dumps(data,ensure_ascii=False))
            self.screen(admin,f'🔐 <b>تأیید ویرایش</b>\n\nمقدار جدید: {esc(content)}\n'
                        'آیا این تغییر ذخیره شود؟',
                        kb([('✅ ثبت تغییر',f'adm:catsave:{data["pid"]}')],[('❌ انصراف','adm:catalog')]))
            return True
        step=data['step']
        try:value=self._validated(step,content)
        except CatalogError as exc:return self.safe_send(admin,'⚠️ '+str(exc)+'\nلطفاً دوباره وارد کنید.')
        data['data'][step]=value
        order=('display','size','price','users','duration')
        if step=='display':
            data['step']='type'
            self.db.set_state(admin,state,json.dumps(data,ensure_ascii=False))
            options=[x['name'] for x in self.catalog_db().categories()]
            data['category_options']=options
            self.db.set_state(admin,state,json.dumps(data,ensure_ascii=False))
            self.screen(admin,'🗂 دسته سرویس را انتخاب کنید.',kb(
                *[[(f'{c}',f'adm:catkind:{i}')] for i,c in enumerate(options)],
                [('🔙 مدیریت سرویس‌ها','adm:catalog')]))
            return True
        nextindex=order.index(step)+1 if step in order else 0
        if nextindex<len(order):
            field=order[nextindex]
            if field=='users' and data['data'].get('type')=='تک کاربره نامحدود':
                data['data']['users']='تک کاربره'
                field='duration'
            data['step']=field
            self.db.set_state(admin,state,json.dumps(data,ensure_ascii=False))
            samples={'size':'حجم به گیگابایت (مثلاً 25)', 'price':'قیمت به تومان (مثلاً 275000)',
                     'users':'تعداد کاربران (مثلاً بدون محدودیت کاربر)',
                     'duration':'مدت اعتبار (مثلاً 30 روز؛ در صورت نامشخص بودن، کلمه «ندارد» را بنویسید)'}
            self.safe_send(admin,f'📝 {samples[field]}')
            return True
        return self.catalog_review(admin,data)

    def _validated(self,field,value):
        from service_catalog import validate
        if field=='duration' and value=='ندارد':value=''
        if field=='size' and type(value)==int:return value
        return validate(field,value)

    def catalog_review(self,admin,data):
        p=data['data']
        if not all(k in p for k in FIELDS):
            self.db.clear_state(admin)
            return self.safe_send(admin,'اطلاعات ناقص است؛ ایجاد سرویس را از ابتدا انجام دهید.')
        self.db.set_state(admin,'catalog_new_confirm',json.dumps(data,ensure_ascii=False))
        text=(f'🔎 <b>پیش‌نمایش سرویس جدید</b>\n\n'
              f'نام: {esc(p["display"])}\nدسته: {esc(p["type"])}\n'
              f'حجم: {p["size"]//1024} GB\nقیمت: {money(p["price"])} تومان\n'
              f'کاربران: {esc(p["users"])}\nاعتبار: {esc(p["duration"] or "—")}\n\n'
              'این سرویس پس از ثبت، در منوی فروش فعال می‌شود.')
        self.screen(admin,text,kb([('✅ ثبت و انتشار','adm:catcreate')],[('❌ انصراف','adm:catalog')]))
        return True

    def category_list(self,admin,msg=None):
        if not self.is_admin(admin):return False
        categories=self.catalog_db().categories()
        rows=[[('🗂 '+c['name'][:34],f'adm:ctview:{c["id"]}')] for c in categories]
        rows += [[('➕ دسته جدید','adm:ctnew')],[('🔙 مدیریت سرویس‌ها','adm:catalog')]]
        return self.screen(admin,f'🗂 <b>مدیریت دسته‌های سرویس</b>\n\n{len(categories)} دسته فعال است.',kb(*rows),msg)

    def category_view(self,admin,cid,msg=None):
        if not self.is_admin(admin):return False
        category=self.catalog_db().category(cid)
        if not category or category['deleted']:
            return self.category_list(admin,msg)
        count=sum(1 for p in self.catalog_db().all() if p['type']==category['name'])
        text=(f'🗂 <b>{esc(category["name"])}</b>\n'
              f'تعداد سرویس‌های این دسته: {count}\n\n'
              'حذف دسته، تمام سرویس‌های آن را از فروشگاه کنار می‌برد و روی سفارش‌های قدیمی اثری ندارد.')
        return self.screen(admin,text,kb(
            [('🗑 حذف کامل دسته و سرویس‌ها',f'adm:ctask:{cid}')],
            [('🔙 فهرست دسته‌ها','adm:ctlist')]),msg)

    def category_callback(self,admin,action,msg=None):
        if not self.is_admin(admin):return False
        if action=='adm:ctlist':
            self.db.clear_state(admin)
            return self.category_list(admin,msg)
        if action=='adm:ctnew':
            return self._ask(admin,'➕ <b>دسته جدید</b>\nنام دسته (مثلاً پنل نقره‌ای) را ارسال کنید.',
                             'catalog_category_new',{},msg)
        parts=action.split(':')
        if len(parts)!=3 or not parts[2].isdigit():return False
        cid=int(parts[2]);cat=self.catalog_db().category(cid)
        if parts[1]=='ctview':return self.category_view(admin,cid,msg)
        if not cat or cat['deleted']:return self.safe_send(admin,'دسته پیدا نشد.')
        if parts[1]=='ctask':
            count=sum(1 for p in self.catalog_db().all() if p['type']==cat['name'])
            state={'cid':cid,'revision':cat['revision'],'nonce':secrets.token_hex(12)}
            self.db.set_state(admin,'catalog_category_confirm',json.dumps(state,ensure_ascii=False))
            return self.screen(admin,f'⚠️ <b>حذف کامل دسته</b>\n\n{esc(cat["name"])}\n'
                f'{count} سرویس این دسته از فهرست فروش حذف می‌شود.\n'
                'سوابق سفارش‌ها باقی می‌مانند. آیا تأیید می‌کنید؟',
                kb([('🗑 بله، حذف دسته و سرویس‌ها',f'adm:ctsave:{cid}')],
                   [('❌ انصراف','adm:ctlist')]),msg)
        if parts[1]=='ctsave':
            state,raw=self.db.get_state(admin)
            if state!='catalog_category_confirm':return self.safe_send(admin,'این تأییدیه منقضی شده است.')
            try:d=json.loads(raw)
            except (ValueError,TypeError):return False
            if d.get('cid')!=cid or not d.get('nonce'):return False
            try:count=self.catalog_db().delete_category(admin,cid,d['revision'])
            except CatalogError as exc:
                self.db.clear_state(admin)
                return self.safe_send(admin,'⚠️ '+str(exc))
            self.db.clear_state(admin)
            self.screen(admin,f'✅ دسته حذف شد و {count} سرویس آن از فروشگاه کنار رفت.',
                        kb([('🗂 دسته‌ها','adm:ctlist')]),msg)
            return True
        return False

    def catalog_callback(self,admin,action,msg=None):
        if not self.is_admin(admin):return False
        if action.startswith('adm:ct'):
            return self.category_callback(admin,action,msg)
        if action=='adm:catalog':
            self.db.clear_state(admin)
            return self.catalog_list(admin,msg=msg)
        if action.startswith('adm:catalog:'):
            value=action.split(':')[-1]
            if value.isdecimal():return self.catalog_list(admin,int(value),msg)
            return True
        if action=='adm:catnew':return self.catalog_new(admin,msg)
        if action.startswith('adm:catdelask:'):
            pid=action[len('adm:catdelask:'):]
            record=self.catalog_db().get(pid)
            if not record or record['deleted']:return self.safe_send(admin,'سرویس پیدا نشد.')
            self.db.set_state(admin,'catalog_delete_confirm',json.dumps({
                'pid':pid,'revision':record['revision'],'nonce':secrets.token_hex(12)},ensure_ascii=False))
            return self.screen(admin,f'⚠️ <b>حذف کامل سرویس</b>\n\n{esc(record["display"])}\n'
                'این سرویس از فروشگاه و فهرست مدیریت کنار می‌رود، اما سفارش‌های قدیمی حفظ می‌شوند.\n'
                'این عملیات با ناموجودکردن متفاوت است. تأیید می‌کنید؟',
                kb([('🗑 حذف کامل سرویس',f'adm:catdelsave:{pid}')],
                   [('❌ انصراف','adm:catalog')]),msg)
        if action.startswith('adm:catdelsave:'):
            pid=action[len('adm:catdelsave:'):]
            state,raw=self.db.get_state(admin)
            if state!='catalog_delete_confirm':return self.safe_send(admin,'این تأییدیه منقضی شده است.')
            try:d=json.loads(raw)
            except (ValueError,TypeError):return False
            if d.get('pid')!=pid or not d.get('nonce'):return False
            try:self.catalog_db().delete_service(admin,pid,d['revision'])
            except CatalogError as exc:
                self.db.clear_state(admin)
                return self.safe_send(admin,'⚠️ '+str(exc))
            self.db.clear_state(admin)
            return self.catalog_list(admin,msg=msg)
        if action.startswith('adm:catkind:'):
            state,raw=self.db.get_state(admin)
            if state!='catalog_new':return self.safe_send(admin,'فرایند افزودن سرویس فعال نیست.')
            try: data=json.loads(raw);idx=int(action.split(':')[-1])
            except (TypeError,ValueError):return True
            options=data.get('category_options',[])
            if data.get('step')!='type' or idx not in range(len(options)):return True
            if options[idx] not in [c['name'] for c in self.catalog_db().categories()]:return self.safe_send(admin,'این دسته دیگر فعال نیست.')
            data['data']['type']=options[idx]
            if options[idx]=='تک کاربره نامحدود':data['data']['users']='تک کاربره'
            data['step']='size'
            self.db.set_state(admin,'catalog_new',json.dumps(data,ensure_ascii=False))
            self.safe_send(admin,'📦 حجم سرویس را به گیگابایت و با اعداد لاتین ارسال کنید. مثال: 25')
            return True
        if action=='adm:catcreate':
            state,raw=self.db.get_state(admin)
            if state!='catalog_new_confirm':return self.safe_send(admin,'این پیش‌نمایش منقضی شده است.')
            try:
                data=json.loads(raw)
                pid=self.catalog_db().create(admin,data['data'],request_id=data['nonce'])
            except (ValueError,KeyError,CatalogError) as exc:return self.safe_send(admin,'⚠️ '+str(exc))
            self.db.clear_state(admin)
            return self.catalog_view(admin,pid,msg)
        parts=action.split(':')
        if len(parts)==3 and parts[1]=='catview':
            self.db.clear_state(admin)
            return self.catalog_view(admin,parts[2],msg)
        if len(parts)==4 and parts[1]=='catedit':
            pid,field=parts[2:]
            record=self.catalog_db().get(pid)
            if not record or field not in FIELDS or record['archived']:return True
            if field=='type':
                self.db.set_state(admin,'catalog_edit',json.dumps({'pid':pid,'field':field,'revision':record['revision']},ensure_ascii=False))
                options=[c['name'] for c in self.catalog_db().categories()]
                state,raw=self.db.get_state(admin)
                draft=json.loads(raw);draft['category_options']=options
                self.db.set_state(admin,'catalog_edit',json.dumps(draft,ensure_ascii=False))
                return self.screen(admin,'🗂 دسته جدید را انتخاب کنید.',kb(
                    *[[(label,f'adm:cattype:{pid}:{idx}')] for idx,label in enumerate(options)],
                    [('🔙 برگشت','adm:catalog')]),msg)
            titles={'price':'قیمت (تومان، فقط عدد لاتین)','display':'عنوان سرویس',
                'size':'حجم (گیگابایت، فقط عدد لاتین)','users':'تعداد کاربران',
                'duration':'مدت اعتبار (مثلاً 30 روز؛ یا ندارد)'}
            return self._ask(admin,f'✏️ مقدار جدید «{titles.get(field,field)}» برای <b>{esc(record["display"])}</b> را بفرستید.',
                 'catalog_edit',{'pid':pid,'field':field,'revision':record['revision']},msg)
        if len(parts)==4 and parts[1]=='cattype':
            pid=parts[2]
            state,raw=self.db.get_state(admin)
            if state!='catalog_edit':return True
            try:data=json.loads(raw);idx=int(parts[3])
            except (TypeError,ValueError):return True
            options=data.get('category_options',[])
            if data.get('pid')!=pid or data.get('field')!='type' or idx not in range(len(options)):return True
            if options[idx] not in [c['name'] for c in self.catalog_db().categories()]:return self.safe_send(admin,'این دسته دیگر فعال نیست.')
            data['value']=options[idx]
            self.db.set_state(admin,'catalog_confirm',json.dumps(data,ensure_ascii=False))
            return self.screen(admin,f'🔐 دسته جدید: {esc(data["value"])}\nثبت تغییر؟',
                 kb([('✅ تأیید تغییر',f'adm:catsave:{pid}')],[('❌ انصراف','adm:catalog')]),msg)
        if len(parts)==4 and parts[1]=='catask':
            pid,field=parts[2:]
            record=self.catalog_db().get(pid)
            if not record or field not in ('available','archived'):return True
            value=(not bool(record[field]))
            self.db.set_state(admin,'catalog_confirm',json.dumps({'pid':pid,'field':field,'value':value,'revision':record['revision']},ensure_ascii=False))
            desc='موجودکردن' if field=='available' and value else 'ناموجودکردن' if field=='available' else 'تغییر وضعیت آرشیو'
            return self.screen(admin,f'🔐 <b>تأیید عملیات</b>\n\n{desc}: {esc(record["display"])}\n'
                        'آیا مطمئن هستید؟',kb([('✅ تأیید',f'adm:catsave:{pid}')],[('❌ انصراف','adm:catalog')]),msg)
        if len(parts)==3 and parts[1]=='catsave':
            pid=parts[2]
            state,raw=self.db.get_state(admin)
            if state!='catalog_confirm':return self.safe_send(admin,'تأیید منقضی شده است.')
            try:data=json.loads(raw)
            except (TypeError,ValueError):return True
            if data.get('pid')!=pid:return True
            try:
                self.catalog_db().change(admin,pid,data['field'],data['value'],data['revision'])
            except (CatalogError,ValueError) as exc:
                self.db.clear_state(admin)
                return self.safe_send(admin,'⚠️ '+str(exc))
            self.db.clear_state(admin)
            return self.catalog_view(admin,pid,msg)
        return False

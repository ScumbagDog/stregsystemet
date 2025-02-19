import datetime
from typing import List

import pytz
from pytz import UTC

from stregreport.views import fjule_party

from django.core import management
from django.forms import modelformset_factory, formset_factory

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.conf import settings
from django.db.models import Q, Count
from django import forms
from django.http import HttpResponsePermanentRedirect, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django_select2 import forms as s2forms
import urllib.parse

from stregsystem import parser
from stregsystem.models import (
    Member,
    Payment,
    News,
    NoMoreInventoryError,
    Order,
    Product,
    Room,
    Sale,
    StregForbudError,
    MobilePayment,
    Category,
)
from stregsystem.utils import (
    make_active_productlist_query,
    qr_code,
    make_room_specific_query,
    make_unprocessed_mobilepayment_query,
    parse_csv_and_create_mobile_payments,
    MobilePaytoolException,
)

from .booze import ballmer_peak
from .caffeine import caffeine_mg_to_coffee_cups
from .forms import MobilePayToolForm, QRPaymentForm, PurchaseForm, RankingDateForm


def __get_news():
    try:
        return News.objects.filter(stop_date__gte=timezone.now(), pub_date__lte=timezone.now()).get()
    except News.DoesNotExist:
        return None


def __get_productlist(room_id):
    return make_active_productlist_query(Product.objects).filter(make_room_specific_query(room_id))


def roomindex(request):
    return HttpResponsePermanentRedirect('/1/')


#    room_list = Room.objects.all().order_by('name', 'description')
#    return render(request, 'stregsystem/roomindex.html', {'room_list': room_list})


def index(request, room_id):
    room = get_object_or_404(Room, pk=int(room_id))
    product_list = __get_productlist(room_id)
    news = __get_news()
    return render(request, 'stregsystem/index.html', locals())


def sale(request, room_id):
    room = get_object_or_404(Room, pk=room_id)
    news = __get_news()
    product_list = __get_productlist(room_id)

    buy_string = request.POST['quickbuy'].strip()
    # Handle empty line
    if buy_string == "":
        return render(request, 'stregsystem/index.html', locals())
    # Extract username and product ids
    try:
        username, bought_ids = parser.parse(buy_string)
    except parser.QuickBuyError as err:
        values = {
            'correct': err.parsed_part,
            'incorrect': err.failed_part,
            'error_ptr': '~' * (len(err.parsed_part)) + '^',
            'error_msg': ' ' * (len(err.parsed_part) - 4) + 'Fejl her',
            'room': room,
        }
        return render(request, 'stregsystem/error_invalidquickbuy.html', values)
    # Fetch member from DB
    try:
        member = Member.objects.get(username=username, active=True)
    except Member.DoesNotExist:
        return render(request, 'stregsystem/error_usernotfound.html', locals())

    if len(bought_ids):
        return quicksale(request, room, member, bought_ids)
    else:
        return usermenu(request, room, member, None)


def _multibuy_hint(now, member):
    # Get a timestamp to fetch sales for the member for the last 60 sec
    earliest_recent_purchase = now - datetime.timedelta(seconds=60)
    # get the sales with this timestamp
    recent_purchases = Sale.objects.filter(member=member, timestamp__gt=earliest_recent_purchase)
    number_of_recent_distinct_purchases = recent_purchases.values('timestamp').distinct().count()

    # add hint for multibuy
    if number_of_recent_distinct_purchases > 1:
        sale_dict = {}
        for sale in recent_purchases:
            if not str(sale.product.id) in sale_dict:
                sale_dict[str(sale.product.id)] = 1
            else:
                sale_dict[str(sale.product.id)] = sale_dict[str(sale.product.id)] + 1
        sale_hints = ["<span class=\"username\">{}</span>".format(member.username)]
        for key in sale_dict:
            if sale_dict[key] > 1:
                sale_hints.append("{}:{}".format(key, sale_dict[key]))
            else:
                sale_hints.append(key)
        return (True, ' '.join(sale_hints))

    return (False, None)


def quicksale(request, room, member: Member, bought_ids):
    news = __get_news()
    product_list = __get_productlist(room.id)
    now = timezone.now()

    # Retrieve products and construct transaction
    products: List[Product] = []
    try:
        for i in bought_ids:
            product = Product.objects.get(
                Q(pk=i),
                Q(active=True),
                Q(deactivate_date__gte=now) | Q(deactivate_date__isnull=True),
                Q(rooms__id=room.id) | Q(rooms=None),
            )
            products.append(product)
    except Product.DoesNotExist:
        return render(request, 'stregsystem/error_productdoesntexist.html', {'failedProduct': i, 'room': room})

    order = Order.from_products(member=member, products=products, room=room)

    try:
        order.execute()
    except StregForbudError:
        return render(request, 'stregsystem/error_stregforbud.html', locals(), status=402)
    except NoMoreInventoryError:
        # @INCOMPLETE this should render with a different template
        return render(request, 'stregsystem/error_stregforbud.html', locals())

    promille = member.calculate_alcohol_promille()
    is_ballmer_peaking, bp_minutes, bp_seconds = ballmer_peak(promille)

    caffeine = member.calculate_caffeine_in_body()
    cups = caffeine_mg_to_coffee_cups(caffeine)
    product_contains_caffeine = any(product.caffeine_content_mg > 0 for product in products)
    is_coffee_master = member.is_leading_coffee_addict()

    cost = order.total

    give_multibuy_hint, sale_hints = _multibuy_hint(now, member)

    return render(request, 'stregsystem/index_sale.html', locals())


def usermenu(request, room, member, bought, from_sale=False):
    negative_balance = member.balance < 0
    product_list = __get_productlist(room.id)
    news = __get_news()
    promille = member.calculate_alcohol_promille()
    (
        is_ballmer_peaking,
        bp_minutes,
        bp_seconds,
    ) = ballmer_peak(promille)

    caffeine = member.calculate_caffeine_in_body()
    cups = caffeine_mg_to_coffee_cups(caffeine)
    is_coffee_master = member.is_leading_coffee_addict()

    give_multibuy_hint, sale_hints = _multibuy_hint(timezone.now(), member)
    give_multibuy_hint = give_multibuy_hint and from_sale

    if member.has_stregforbud():
        return render(request, 'stregsystem/error_stregforbud.html', locals())
    else:
        return render(request, 'stregsystem/menu.html', locals())


def menu_userinfo(request, room_id, member_id):
    room = Room.objects.get(pk=room_id)
    news = __get_news()
    member = Member.objects.get(pk=member_id, active=True)

    last_sale_list = member.sale_set.order_by('-timestamp')[:10]
    try:
        last_payment = member.payment_set.order_by('-timestamp')[0]
    except IndexError:
        last_payment = None

    negative_balance = member.balance < 0
    stregforbud = member.has_stregforbud()

    return render(request, 'stregsystem/menu_userinfo.html', locals())


def menu_userpay(request, room_id, member_id):
    room = Room.objects.get(pk=room_id)
    member = Member.objects.get(pk=member_id, active=True)

    amounts = {100, 200}

    try:
        last_payment = member.payment_set.order_by('-timestamp')[0]
        amounts.add(last_payment.amount / 100.0)
    except IndexError:
        last_payment = None

    negative_balance = member.balance < 0
    if negative_balance:
        amounts.add(-member.balance / 100.0)

    amounts = sorted(amounts)

    return render(request, 'stregsystem/menu_userpay.html', locals())


def menu_userrank(request, room_id, member_id):
    from_date = fjule_party(datetime.datetime.today().year - 1)
    to_date = datetime.datetime.now(tz=pytz.timezone("Europe/Copenhagen"))
    room = Room.objects.get(pk=room_id)
    member = Member.objects.get(pk=member_id, active=True)

    def ranking(category_ids, from_d, to_d):
        qs = (
            Member.objects.filter(sale__product__in=category_ids, sale__timestamp__gt=from_d, sale__timestamp__lte=to_d)
            .annotate(Count('sale'))
            .order_by('-sale__count', 'username')
        )
        if member not in qs:
            return 0, qs.count()
        return list(qs).index(Member.objects.get(id=member.id)) + 1, int(qs.count())

    def get_product_ids_for_category(category) -> list:
        return list(
            Product.objects.filter(categories__exact=Category.objects.get(name__exact=category)).values_list(
                'id', flat=True
            )
        )

    def category_per_uni_day(category_ids, from_d, to_d):
        qs = Member.objects.filter(
            id=member.id,
            sale__product__in=category_ids,
            sale__timestamp__gt=from_d,
            sale__timestamp__lte=to_d,
        )
        if member not in qs:
            return 0
        else:
            return "{:.2f}".format(qs.count() / ((to_d - from_d).days * 162.14 / 365))  # university workdays in 2021

    # let user know when they first purchased a product
    member_first_purchase = "Ikke endnu, køb en limfjordsporter!"
    first_purchase = Sale.objects.filter(member=member_id).order_by('-timestamp')
    if first_purchase.exists():
        member_first_purchase = first_purchase.last().timestamp

    form = RankingDateForm()
    if request.method == "POST" and request.POST['custom-range']:
        form = RankingDateForm(request.POST)
        if form.is_valid():
            from_date = form.cleaned_data['from_date']
            to_date = form.cleaned_data['to_date']
    else:
        # setup initial dates for form and results
        form = RankingDateForm(initial={'from_date': from_date, 'to_date': to_date})

    # get prod_ids for each category as dict {cat: [key1, key2])}, then flatten list of singleton
    # dicts into one dict, lastly calculate member_id rating and units/weekday for category_ids
    rankings = {
        key: (
            ranking(category_ids, from_date, to_date),
            category_per_uni_day(category_ids, from_date, to_date),
        )
        for key, category_ids in {
            k: v
            for x in map(lambda x: {x: get_product_ids_for_category(x)}, list(Category.objects.all()))
            for k, v in x.items()
        }.items()
    }

    return render(request, 'stregsystem/menu_userrank.html', locals())


def menu_sale(request, room_id, member_id, product_id=None):
    room = Room.objects.get(pk=room_id)
    news = __get_news()
    member = Member.objects.get(pk=member_id, active=True)

    product = None
    if request.method == 'POST':
        purchase = PurchaseForm(request.POST)
        if not purchase.is_valid():
            return HttpResponseBadRequest(
                "Cannot complete sale, get help at /dev/null or at mailto:[treo|fit]@fklub.dk"
            )

        try:
            product = Product.objects.get(
                Q(pk=purchase.cleaned_data['product_id']),
                Q(active=True),
                Q(rooms__id=room_id) | Q(rooms=None),
                Q(deactivate_date__gte=timezone.now()) | Q(deactivate_date__isnull=True),
            )

            order = Order.from_products(member=member, room=room, products=(product,))

            order.execute()

        except Product.DoesNotExist:
            pass
        except StregForbudError:
            return render(request, 'stregsystem/error_stregforbud.html', locals())
        except NoMoreInventoryError:
            # @INCOMPLETE this should render with a different template
            return render(request, 'stregsystem/error_stregforbud.html', locals())

    # Refresh member, to get new amount
    member = Member.objects.get(pk=member_id, active=True)
    return usermenu(request, room, member, product, from_sale=True)


@staff_member_required()
@permission_required("stregsystem.import_batch_payments")
def batch_payment(request):
    PaymentFormSet = forms.modelformset_factory(
        Payment, fields=("member", "amount"), widgets={"member": forms.Select(attrs={"class": "select2"})}
    )
    if request.method == "POST":
        formset = PaymentFormSet(request.POST, request.FILES)
        if formset.is_valid():
            # @HACK For some reason django formsets get different member
            # instances with the same values instead of the same instance. This
            # means that if we update the member balance and save it, then
            # repeat, the second write will overwrite the first. In case you
            # don't know, that's not good.
            # To work around this we have to set all the member instances to be
            # the same if they have the same id. This is very, although
            # slightly less, bad - Jesper Jensen 16/02-2017
            payments = formset.save(commit=False)

            # @HACK: We need to consolidate all the member so they are the same
            # instances. We do this by just saving the first one and setting
            # all the remaining with the same id to be that instance aswell.
            members = {}
            for payment in payments:
                if payment.member.id not in members:
                    members[payment.member.id] = payment.member
                payment.member = members[payment.member.id]

            for payment in payments:
                payment.save()

            return render(request, "admin/stregsystem/batch_payment_done.html", {})
    else:
        formset = PaymentFormSet(queryset=Payment.objects.none())
    return render(
        request,
        "admin/stregsystem/batch_payment.html",
        {
            "formset": formset,
            "select2_js": settings.SELECT2_JS,
            "select2_css": settings.SELECT2_CSS,
        },
    )


@staff_member_required()
@permission_required("stregsystem.mobilepaytool_access")
def mobilepaytool(request):
    paytool_form_set = modelformset_factory(
        MobilePayment, form=MobilePayToolForm, extra=0, fields=('timestamp', 'amount', 'member', 'comment', 'status')
    )  # TODO: 'customer_name' removed, MobilepayAPI does not
    # TODO-cont: have that information at this point in time - add back 'customer_name' if available in future
    data = dict()
    if request.method == "GET":
        data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())
    elif request.method == "POST" and 'csv_file' in request.FILES and request.POST['action'] == "Import MobilePay CSV":
        # Prepare uploaded CSV to be read
        csv_file = request.FILES['csv_file']
        csv_file.seek(0)

        data['imports'], data['duplicates'] = parse_csv_and_create_mobile_payments(
            str(csv_file.read().decode('utf-8')).splitlines()
        )

        # refresh form after submission
        data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())

    elif request.method == "POST" and request.POST['action'] == "Import via MobilePay API":
        before_count = MobilePayment.objects.count()
        management.call_command('importmobilepaypayments')
        count = MobilePayment.objects.count() - before_count

        data['api'] = f"Successfully imported {count} MobilePay transactions"
        data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())

    elif request.method == "POST" and request.POST['action'] == "Submit matched payments":
        before_count = MobilePayment.objects.filter(status=MobilePayment.APPROVED).count()
        MobilePayment.approve_member_filled_mobile_payments()
        MobilePayment.submit_processed_mobile_payments(request.user)
        count = MobilePayment.objects.filter(status=MobilePayment.APPROVED).count() - before_count

        data['submitted_count'] = count
        data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())

    elif request.method == "POST" and request.POST['action'] == "Submit payments":
        form = paytool_form_set(request.POST)

        if form.is_valid():
            try:
                # Do custom validation on form to avoid race conditions with autopayment
                count = MobilePayment.process_submitted_mobile_payments(form.cleaned_data, request.user)
                data['submitted_count'] = count
            except MobilePaytoolException as e:
                data['error_count'] = e.inconsistent_mbpayments_count
                data['error_transaction_ids'] = e.inconsistent_transaction_ids

            # refresh form after submission
            data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())
        else:
            # update form with errors
            data['formset'] = form
    else:
        data['formset'] = paytool_form_set(queryset=make_unprocessed_mobilepayment_query())

    return render(request, "admin/stregsystem/mobilepaytool.html", data)


def qr_payment(request):
    form = QRPaymentForm(request.GET)
    if not form.is_valid():
        return HttpResponseBadRequest("Invalid input for MobilePay QR code generation")

    query = {'phone': '90601', 'comment': form.cleaned_data.get('member')}

    if form.cleaned_data.get("amount") is not None:
        query['amount'] = form.cleaned_data.get("amount")

    data = 'mobilepay://send?{}'.format(urllib.parse.urlencode(query))

    return qr_code(data)

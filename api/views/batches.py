from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.orders.models import DeliveryBatch, Order
from api.serializers.core import OrderSerializer
from domain.services.orders.order_assignment import AssignBatchService


class BatchViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if user.serialized_role == 'driver':
            return DeliveryBatch.objects.filter(driver__user=user)
        elif user.serialized_role in ('vendor', 'vendor_staff'):
            from apps.restaurants.models import Restaurant, SectionStaff
            owned_rids = Restaurant.objects.filter(owner=user).values_list('id', flat=True)
            staff_rids = SectionStaff.objects.filter(user=user).values_list('section__restaurant_id', flat=True)
            all_rids = set(owned_rids) | set(staff_rids)
            return DeliveryBatch.objects.filter(orders__restaurant_id__in=all_rids).distinct()
        return DeliveryBatch.objects.none()
    
    def list(self, request):
        queryset = self.get_queryset()
        from apps.orders.models import Order
        orders = Order.objects.filter(batch__in=queryset).distinct()
        return Response(OrderSerializer(orders, many=True).data)
    
    def retrieve(self, request, pk=None):
        queryset = self.get_queryset()
        try:
            batch = queryset.get(id=pk)
            orders = batch.orders.all()
            return Response(OrderSerializer(orders, many=True).data)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found or access denied'}, status=404)
    
    @action(detail=True, methods=['post'], url_path='assign-driver')
    def assign_driver(self, request, pk=None):
        """Restaurant owner or stall manager assigns a driver to a batch."""
        if request.user.role not in ('vendor',):
            return Response({'error': 'Only vendors or stall managers can assign drivers'}, status=403)

        driver_id = request.data.get('driver_id')
        if not driver_id:
            return Response({'error': 'driver_id is required'}, status=400)

        try:
            batch = AssignBatchService.execute(str(pk), str(driver_id), request.user)
            orders = batch.orders.all()
            return Response(OrderSerializer(orders, many=True).data)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            return Response({'error': f'Failed to assign driver: {str(e)}'}, status=400)

    @action(detail=True, methods=['get'], url_path='metadata')
    def batch_metadata(self, request, pk=None):
        """Get batch-level summary with progress"""
        try:
            batch = DeliveryBatch.objects.get(id=pk)
            orders = batch.orders.select_related('restaurant', 'customer').all()
            
            delivered = orders.filter(status='DELIVERED').count()
            total = orders.count()
            
            role = getattr(request, 'user', None).role if hasattr(request, 'user') and request.user else None

            result = {
                'id': str(batch.id),
                'status': batch.status,
                'batch_type': batch.batch_type,
                'total_orders': total,
                'delivered_count': delivered,
                'remaining_count': total - delivered,
                'route_distance_km': float(batch.route_distance_km) if batch.route_distance_km else None,
                'route_optimization': batch.route_optimization,
            }

            if role == 'admin':
                result['driver_payout'] = float(batch.driver_payout) if batch.driver_payout else None
                result['user_paid_total'] = float(batch.user_paid_total) if batch.user_paid_total else None
                result['company_profit'] = float(batch.company_profit) if batch.company_profit else None
            elif role == 'driver':
                result['driver_payout'] = float(batch.driver_payout) if batch.driver_payout else None
            elif role in ('vendor', 'customer'):
                result['user_paid_total'] = float(batch.user_paid_total) if batch.user_paid_total else None

            result.update({
                'restaurants': [
                    {
                        'id': str(o.restaurant.id),
                        'name': o.restaurant.name,
                        'latitude': o.restaurant.geo_location.y if o.restaurant and o.restaurant.geo_location else None,
                        'longitude': o.restaurant.geo_location.x if o.restaurant and o.restaurant.geo_location else None,
                    }
                    for o in orders.select_related('restaurant').order_by('route_sequence')
                    if o.restaurant
                ],
                'orders_summary': [
                    {
                        'id': str(o.id),
                        'route_sequence': o.route_sequence,
                        'status': o.status,
                        'customer_name': o.customer.username,
                        'customer_phone': o.customer.phone,
                        'delivery_address': o.delivery_address,
                        'delivery_latitude': o.delivery_location.y if o.delivery_location else None,
                        'delivery_longitude': o.delivery_location.x if o.delivery_location else None,
                        'restaurant_name': o.restaurant.name if o.restaurant else 'Marketplace',
                        'restaurant_latitude': o.restaurant.geo_location.y if o.restaurant and o.restaurant.geo_location else None,
                        'restaurant_longitude': o.restaurant.geo_location.x if o.restaurant and o.restaurant.geo_location else None,
                    }
                    for o in orders.order_by('route_sequence')
                ],
            })

            return Response(result)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)

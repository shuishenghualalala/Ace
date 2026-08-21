use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use crate::gatt::peripheral_event::{
    PeripheralEvent, PeripheralRequest, ReadRequestResponse, RequestResponse, WriteRequestResponse,
};
use crate::peripheral::winrt::win_utils::{
    buffer_to_vec, device_id_from_session, to_uuid, vec_to_buffer,
};
use tokio::sync::mpsc::Sender;
use tokio::sync::oneshot;
use uuid::Uuid;
use windows::core::IInspectable;
use windows::Devices::Bluetooth::GenericAttributeProfile::{
    GattProtocolError, GattServiceProviderAdvertisementStatus, GattSubscribedClient,
    GattWriteOption,
};
use windows::Devices::Radios::{Radio, RadioState};
use windows::Foundation::Collections::IVectorView;
use windows::{
    Devices::Bluetooth::GenericAttributeProfile::{
        GattLocalCharacteristic, GattReadRequestedEventArgs, GattServiceProvider,
        GattServiceProviderAdvertisementStatusChangedEventArgs, GattWriteRequestedEventArgs,
    },
    Foundation::TypedEventHandler,
};

pub struct WinEventHandler {
    sender_tx: Sender<PeripheralEvent>,
    connected_clients: Arc<RwLock<HashMap<(Uuid, Uuid), Vec<String>>>>,
}

impl WinEventHandler {
    pub fn new(sender_tx: Sender<PeripheralEvent>) -> Self {
        Self {
            sender_tx,
            connected_clients: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    pub fn create_radio_listener(&self) -> TypedEventHandler<Radio, IInspectable> {
        let sender_tx: Sender<PeripheralEvent> = self.sender_tx.clone();

        return TypedEventHandler::new(
            move |originator: &Option<Radio>, _: &Option<IInspectable>| {
                let radio = originator.as_ref().unwrap();
                let is_on = radio.State().unwrap() == RadioState::On;
                futures::executor::block_on(async {
                    if let Err(err) = sender_tx
                        .send(PeripheralEvent::StateUpdate { is_powered: is_on })
                        .await
                    {
                        log::error!("Error sending delegate event: {}", err);
                    }
                });
                Ok(())
            },
        );
    }

    pub fn create_advertisement_status_handler(
        &self,
    ) -> TypedEventHandler<
        GattServiceProvider,
        GattServiceProviderAdvertisementStatusChangedEventArgs,
    > {
        TypedEventHandler::new(move |originator: &Option<GattServiceProvider>, args: &Option<GattServiceProviderAdvertisementStatusChangedEventArgs>| {
            let service = originator.as_ref().unwrap();
            let event_args = args.as_ref().unwrap();
            let status = event_args.Status()?;
            log::debug!("Advertisement Status: {:?}: Started: {:?}",
                to_uuid(&service.Service().unwrap().Uuid().unwrap()),
                status == GattServiceProviderAdvertisementStatus::Started);
            Ok(())
        })
    }

    pub fn create_subscribe_handler(
        &self,
        service_uuid: Uuid,
    ) -> TypedEventHandler<GattLocalCharacteristic, IInspectable> {
        let connected_clients = Arc::clone(&self.connected_clients);
        let sender_tx: Sender<PeripheralEvent> = self.sender_tx.clone();

        TypedEventHandler::new(
            move |originator: &Option<GattLocalCharacteristic>, _: &Option<IInspectable>| {
                let characteristic: &GattLocalCharacteristic = originator.as_ref().unwrap();
                let characteristic_uuid = to_uuid(&characteristic.Uuid().unwrap());

                let subscribed_clients: IVectorView<GattSubscribedClient> =
                    characteristic.SubscribedClients().unwrap();

                let new_clients: Vec<String> = subscribed_clients
                    .into_iter()
                    .map(|client| device_id_from_session(client.Session().unwrap()))
                    .collect();

                let mut old_clients_store = connected_clients.write().unwrap();
                let mut added_clients: Vec<String> = Vec::new();
                let mut removed_clients: Vec<String> = Vec::new();

                if let Some(old_clients) = old_clients_store
                    .get_mut(&(service_uuid, to_uuid(&characteristic.Uuid().unwrap())))
                {
                    for client in &new_clients {
                        if !old_clients.contains(client) {
                            added_clients.push(client.clone());
                        }
                    }
                    for client in old_clients.clone() {
                        if !new_clients.contains(&client) {
                            removed_clients.push(client.clone());
                        }
                    }

                    *old_clients = new_clients;
                } else {
                    old_clients_store
                        .insert((service_uuid, characteristic_uuid), new_clients.clone());
                    added_clients.extend(new_clients.clone());
                }

                // Update Newly added/removed clients
                futures::executor::block_on(async {
                    for client in added_clients {
                        if let Err(err) = sender_tx
                            .send(PeripheralEvent::CharacteristicSubscriptionUpdate {
                                request: PeripheralRequest {
                                    client,
                                    service: service_uuid,
                                    characteristic: characteristic_uuid,
                                },
                                subscribed: true,
                            })
                            .await
                        {
                            log::error!("Error sending delegate event: {}", err);
                        }
                    }

                    for client in removed_clients {
                        if let Err(err) = sender_tx
                            .send(PeripheralEvent::CharacteristicSubscriptionUpdate {
                                request: PeripheralRequest {
                                    client,
                                    service: service_uuid,
                                    characteristic: characteristic_uuid,
                                },
                                subscribed: false,
                            })
                            .await
                        {
                            log::error!("Error sending delegate event: {}", err);
                        }
                    }
                });
                Ok(())
            },
        )
    }

    pub fn create_read_handler(
        &mut self,
        service_uuid: Uuid,
    ) -> TypedEventHandler<GattLocalCharacteristic, GattReadRequestedEventArgs> {
        let sender_tx: Sender<PeripheralEvent> = self.sender_tx.clone();
        let runtime = tokio::runtime::Handle::current();

        TypedEventHandler::new(
            move |originator: &Option<GattLocalCharacteristic>,
                  args: &Option<GattReadRequestedEventArgs>| {
                let Some(event_args) = args.as_ref().cloned() else {
                    return Ok(());
                };
                let Some(characteristic) = originator.as_ref() else {
                    return Ok(());
                };
                let deferral = event_args.GetDeferral()?;
                let characteristic_uuid = to_uuid(&characteristic.Uuid()?);
                let client = device_id_from_session(event_args.Session()?);
                let sender_tx = sender_tx.clone();

                runtime.spawn(async move {
                    let result = async {
                        let request = event_args
                            .GetRequestAsync()
                            .map_err(|error| error.to_string())?
                            .await
                            .map_err(|error| error.to_string())?;
                        let offset = request.Offset().map_err(|error| error.to_string())? as u64;
                        let (resp_tx, resp_rx) = oneshot::channel::<ReadRequestResponse>();
                        sender_tx
                            .send(PeripheralEvent::ReadRequest {
                                request: PeripheralRequest {
                                    client,
                                    service: service_uuid,
                                    characteristic: characteristic_uuid,
                                },
                                offset,
                                responder: resp_tx,
                            })
                            .await
                            .map_err(|error| error.to_string())?;
                        let response = resp_rx.await.map_err(|error| error.to_string())?;
                        if response.response == RequestResponse::Success {
                            request
                                .RespondWithValue(&vec_to_buffer(response.value))
                                .map_err(|error| error.to_string())?;
                        } else {
                            request
                                .RespondWithProtocolError(
                                    response.response.to_gatt_protocol_error(),
                                )
                                .map_err(|error| error.to_string())?;
                        }
                        Ok::<(), String>(())
                    }
                    .await;
                    if let Err(error) = result {
                        log::error!("Failed to handle GATT read request: {}", error);
                    }
                    if let Err(error) = deferral.Complete() {
                        log::error!("Failed to complete GATT read deferral: {}", error);
                    }
                });

                Ok(())
            },
        )
    }

    pub fn create_write_handler(
        &self,
        service_uuid: Uuid,
    ) -> TypedEventHandler<GattLocalCharacteristic, GattWriteRequestedEventArgs> {
        let sender_tx = self.sender_tx.clone();
        let runtime = tokio::runtime::Handle::current();

        TypedEventHandler::new(
            move |originator: &Option<GattLocalCharacteristic>,
                  args: &Option<GattWriteRequestedEventArgs>| {
                let Some(event_args) = args.as_ref().cloned() else {
                    return Ok(());
                };
                let Some(characteristic) = originator.as_ref() else {
                    return Ok(());
                };
                let deferral = event_args.GetDeferral()?;
                let characteristic_uuid = to_uuid(&characteristic.Uuid()?);
                let client = device_id_from_session(event_args.Session()?);
                let sender_tx = sender_tx.clone();

                runtime.spawn(async move {
                    let result = async {
                        let request = event_args
                            .GetRequestAsync()
                            .map_err(|error| error.to_string())?
                            .await
                            .map_err(|error| error.to_string())?;
                        let with_response = request
                            .Option()
                            .map_err(|error| error.to_string())?
                            == GattWriteOption::WriteWithResponse;
                        let value = buffer_to_vec(
                            &request.Value().map_err(|error| error.to_string())?,
                        );
                        let offset = request.Offset().map_err(|error| error.to_string())? as u64;
                        let (resp_tx, resp_rx) = oneshot::channel::<WriteRequestResponse>();
                        sender_tx
                            .send(PeripheralEvent::WriteRequest {
                                request: PeripheralRequest {
                                    client,
                                    service: service_uuid,
                                    characteristic: characteristic_uuid,
                                },
                                value,
                                offset,
                                responder: resp_tx,
                            })
                            .await
                            .map_err(|error| error.to_string())?;
                        let response = resp_rx.await.map_err(|error| error.to_string())?;
                        if with_response {
                            if response.response == RequestResponse::Success {
                                request.Respond().map_err(|error| error.to_string())?;
                            } else {
                                request
                                    .RespondWithProtocolError(
                                        response.response.to_gatt_protocol_error(),
                                    )
                                    .map_err(|error| error.to_string())?;
                            }
                        }
                        Ok::<(), String>(())
                    }
                    .await;
                    if let Err(error) = result {
                        log::error!("Failed to handle GATT write request: {}", error);
                    }
                    if let Err(error) = deferral.Complete() {
                        log::error!("Failed to complete GATT write deferral: {}", error);
                    }
                });

                Ok(())
            },
        )
    }
}

impl RequestResponse {
    fn to_gatt_protocol_error(self) -> u8 {
        let result = match self {
            RequestResponse::Success => Ok(0),
            RequestResponse::InvalidHandle => GattProtocolError::InvalidHandle(),
            RequestResponse::RequestNotSupported => GattProtocolError::RequestNotSupported(),
            RequestResponse::InvalidOffset => GattProtocolError::InvalidOffset(),
            RequestResponse::UnlikelyError => GattProtocolError::UnlikelyError(),
        };
        if let Ok(value) = result {
            return value;
        }
        return 0;
    }
}
